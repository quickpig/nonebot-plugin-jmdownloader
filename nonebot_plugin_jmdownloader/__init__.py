import asyncio
import hashlib
import os
import random
import shutil
import struct
import time
from io import BytesIO
from pathlib import Path

from jmcomic import (JmcomicException, JmDownloader,
                     MissingAlbumPhotoException, create_option_by_str)
from nonebot import logger, on_command, require
from nonebot.adapters.onebot.v11 import (GROUP_ADMIN, GROUP_OWNER,
                                         ActionFailed, Bot, GroupMessageEvent,
                                         Message, MessageEvent, MessageSegment,
                                         PrivateMessageEvent)
from nonebot.params import ArgPlainText, CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from .config import (Config, cache_dir, config_data, plugin_cache_dir,
                     plugin_config)
from .data_source import data_manager
from .utils import (blur_image_async, check_group_and_user, check_permission,
                    download_avatar, download_photo_async, modify_pdf_md5,
                    get_photo_info_async, search_album_async,
                    send_forward_message)

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

__plugin_meta__ = PluginMetadata(
    name="JMComic插件",
    description="JMComic搜索、下载插件，支持全局屏蔽jm号和tag，仅支持OnebotV11协议。",
    usage="jm下载 [jm号]：下载指定jm号的本子\n"
          "jm查询 [jm号]：查询指定jm号的本子\n"
          "jm搜索 [关键词]：搜索包含关键词的本子\n"
          "jm设置文件夹 [文件夹名]：设置本群的本子储存文件夹\n",
    type="application",  # library
    homepage="https://github.com/Misty02600/nonebot-plugin-jmdownloader",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={"author": "Misty02600 <xiao02600@gmail.com>"},
)

option = create_option_by_str(config_data, mode="yml")

try:
    client = option.build_jm_client()
    downloader = JmDownloader(option)
except JmcomicException as e:
    logger.error(f"初始化失败: { e }")

# 添加用于检查搜索关键词的函数
def check_search_keywords(search_query: str) -> bool:
    """
    检查搜索关键词是否包含禁止的关键词
    
    Args:
        search_query: 搜索关键词
    
    Returns:
        bool: 如果包含禁止关键词返回True，否则返回False
    """
    # 从data_manager获取restricted_tags作为关键词检查基础
    restricted_tags = data_manager.data.setdefault("restricted_tags", [])
    
    # 将搜索关键词转为小写进行比较
    search_query_lower = search_query.lower()
    
    # 检查是否包含任何禁止关键词
    for tag in restricted_tags:
        if tag.lower() in search_query_lower:
            return True
    
    return False

# region jm功能指令
jm_download = on_command("jm下载", aliases={"JM下载"}, block=True, rule=check_group_and_user)
@jm_download.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    photo_id = arg.extract_plain_text().strip()
    user_id = event.user_id

    if not photo_id.isdigit():
        await jm_download.finish("请输入要下载的jm号")

    if str(user_id) not in bot.config.superusers:
        user_limit = data_manager.get_user_limit(user_id)
        if user_limit <= 0:
            await jm_download.finish(MessageSegment.at(user_id) + f"你的下载次数已经用完了！")

    photo = await get_photo_info_async(client, photo_id)
    if photo == 0:
        await jm_download.finish("未查找到本子")
    if photo is None:
        await jm_download.finish("查询时发生错误")

    if data_manager.is_jm_id_restricted(photo.id) or data_manager.has_restricted_tag(photo.tags):
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.set_group_ban(group_id=event.group_id, user_id=user_id, duration=86400)
            except ActionFailed:
                pass
            data_manager.add_blacklist(event.group_id, user_id)
            await jm_download.finish(MessageSegment.at(user_id) + "该本子（或其tag）被禁止下载!你已被加入本群jm黑名单")
        else:
            await jm_download.finish("该本子（或其tag）被禁止下载！")


    if str(user_id) not in bot.config.superusers:
        data_manager.decrease_user_limit(user_id, 1)
        user_limit_new = data_manager.get_user_limit(user_id)
        await jm_download.send(
            f"查询到jm{photo.id}: {photo.title}\ntags:{photo.tags}\n开始下载...你本周还有{user_limit_new}次下载次数！"
        )
    else:
        await jm_download.send(f"查询到jm{photo.id}: {photo.title}\ntags:{photo.tags}\n开始下载...")

    try:
        # 检查PDF是否已存在
        original_pdf_path = f"{cache_dir}/{photo.id}.pdf"
        
        # 如果不存在，则下载
        if not os.path.exists(original_pdf_path):
            if not await download_photo_async(client, downloader, photo):
                await jm_download.finish("下载失败")
        
        # 生成随机后缀
        random_suffix = hashlib.md5(str(time.time() + random.random()).encode()).hexdigest()[:8]
        renamed_pdf_path = f"{cache_dir}/{photo.id}_{random_suffix}.pdf"
        
        # 根据配置决定是否真正修改MD5
        if plugin_config.jmcomic_modify_real_md5:
            # 修改文件内容的MD5
            if not modify_pdf_md5(original_pdf_path, renamed_pdf_path):
                # 如果修改失败，退回到复制方案
                shutil.copy2(original_pdf_path, renamed_pdf_path)
        else:
            # 仅复制文件并重命名
            shutil.copy2(original_pdf_path, renamed_pdf_path)

        try:
            if isinstance(event, GroupMessageEvent):
                folder_id = data_manager.get_group_folder_id(event.group_id)

                if folder_id:
                    await bot.call_api(
                        "upload_group_file",
                        group_id=event.group_id,
                        file=renamed_pdf_path,
                        name=f"{photo.id}.pdf",  # 显示名称仍然保持原样
                        folder_id=folder_id
                    )
                else:
                    await bot.call_api(
                        "upload_group_file",
                        group_id=event.group_id,
                        file=renamed_pdf_path,
                        name=f"{photo.id}.pdf"
                    )

            elif isinstance(event, PrivateMessageEvent):
                await bot.call_api(
                    "upload_private_file",
                    user_id=event.user_id,
                    file=renamed_pdf_path,
                    name=f"{photo.id}.pdf"
                )

            # 删除临时重命名的文件
            os.remove(renamed_pdf_path)

        except ActionFailed:
            # 清理临时文件
            if os.path.exists(renamed_pdf_path):
                os.remove(renamed_pdf_path)
            await jm_download.finish("发送文件失败")

    except Exception as e:
        logger.error(f"处理PDF文件时出错: {e}")
        await jm_download.finish("处理文件失败")


jm_query = on_command("jm查询", aliases={"JM查询"}, block=True, rule=check_group_and_user)
@jm_query.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    photo_id = arg.extract_plain_text().strip()

    if not photo_id.isdigit():
        await jm_query.finish("请输入要查询的jm号")

    try:
        photo = await get_photo_info_async(client, photo_id)
    except MissingAlbumPhotoException:
        await jm_query.finish("未查找到本子")

    if photo is None:
        await jm_query.finish("查询时发生错误")


    message = Message(f'查询到jm{photo.id}: {photo.title}\ntags:{photo.tags}')
    avatar = await download_avatar(photo.id)

    if avatar:
        avatar = await blur_image_async(avatar)
        message += MessageSegment.image(avatar)

    message_node = MessageSegment("node", {"name": "jm查询结果", "content": message})
    messages = [message_node]

    try:
        await send_forward_message(bot, event, messages)
    except ActionFailed:
        await jm_query.finish("查询结果发送失败", reply_message=True)


jm_search = on_command("jm搜索", aliases={"JM搜索"}, block=True, rule=check_group_and_user)
@jm_search.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    search_query = arg.extract_plain_text().strip()
    user_id = event.user_id

    if not search_query:
        await jm_search.finish("请输入要搜索的内容")

    # 检查搜索关键词是否包含禁止的标签
    is_blocked = check_search_keywords(search_query)
    if is_blocked:
        blocked_message = plugin_config.jmcomic_blocked_message
        await jm_search.finish(blocked_message)

    searching_msg_id = (await jm_search.send("正在搜索中..."))['message_id']

    # 使用原有的搜索函数
    page = await search_album_async(client, search_query)

    if page is None:
        await jm_search.finish("搜索失败", reply_message=True)
        
    if not page:
        await bot.delete_msg(message_id=searching_msg_id)
        await jm_search.finish("未搜索到本子", reply_message=True)

    # 将搜索结果转换为列表，确保可以访问
    search_results = list(page)
    
    # 保存完整的搜索结果到用户状态
    data_manager.save_search_state(user_id, search_query, 0, search_results)
    
    # 记录日志
    logger.debug(f"用户 {user_id} 搜索 '{search_query}' 共找到 {len(search_results)} 条结果")
    
    # 只显示前10个结果
    results_per_page = 10
    current_results = search_results[:results_per_page]

    # 获取详细信息和头像
    album_details = await asyncio.gather(*(get_photo_info_async(client, album_id) for album_id, _ in current_results))
    avatars = await asyncio.gather(*(download_avatar(album_id) for album_id, _ in current_results))

    # 准备显示消息列表
    messages = []
    blocked_message = plugin_config.jmcomic_blocked_message
    
    for (album_id, title), photo, avatar in zip(current_results, album_details, avatars):
        # 检查标签是否应该被屏蔽
        if photo and hasattr(photo, 'tags') and photo.tags:
            # 使用data_manager.has_restricted_tag替代blocked_tags检查
            if data_manager.has_restricted_tag(photo.tags):
                # 添加屏蔽提示到转发消息中
                message = Message(blocked_message)
                message_node = MessageSegment("node", {"name": "jm搜索结果", "content": message})
                messages.append(message_node)
                continue  # 处理下一个结果
                
        # 构建包含详细信息的消息
        message = Message()
        message += f"jm{album_id} | {title}\n"
        
        # 添加作者信息（如果有）
        if photo and hasattr(photo, 'author') and photo.author:
            message += f"👤 作者: {photo.author}\n"
        
        # 添加分类信息（如果有）
        if photo and hasattr(photo, 'category') and photo.category:
            category_info = f"📂 分类: {photo.category.title if hasattr(photo.category, 'title') else '未分类'}"
            if hasattr(photo, 'category_sub') and photo.category_sub and hasattr(photo.category_sub, 'title'):
                if photo.category_sub.title and photo.category_sub.title != photo.category.title:
                    category_info += f" > {photo.category_sub.title}"
            message += category_info + "\n"
        
        # 添加标签信息（如果有）
        if photo and hasattr(photo, 'tags') and photo.tags:
            tag_lines = []
            current_line = "🏷️ 标签: "
            tag_count = 0
            
            for tag in photo.tags:
                if tag_count > 0 and tag_count % 4 == 0:
                    tag_lines.append(current_line)
                    current_line = "          "
                current_line += f"#{tag} "
                tag_count += 1
                
            if current_line != "          ":
                tag_lines.append(current_line)
                
            message += "\n".join(tag_lines)

        # 添加封面图片
        if avatar:
            avatar = await blur_image_async(avatar)
            message += MessageSegment.image(avatar)

        message_node = MessageSegment("node", {"name": "jm搜索结果", "content": message})
        messages.append(message_node)

    await bot.delete_msg(message_id=searching_msg_id)

    try:
        await send_forward_message(bot, event, messages)
        
        # 提示用户可以查看更多结果
        if len(search_results) > results_per_page:
            await jm_search.finish(f"搜索有更多结果，输入\"jm下一页\"查看更多")
        else:
            await jm_search.finish(f"已显示所有搜索结果")
    except ActionFailed:
        await jm_search.finish("搜索结果发送失败", reply_message=True)


# 4. 添加下一页功能 - 修改为从当前页开始，而不是从第一页重新开始
jm_next_page = on_command("jm 下一页", aliases={"JM 下一页", "jm下一页", "JM下一页"}, block=True, rule=check_group_and_user)
@jm_next_page.handle()
async def handle_jm_next_page(bot: Bot, event: MessageEvent):
    """处理下一页请求"""
    user_id = event.user_id
    search_state = data_manager.get_search_state(user_id)
    
    if not search_state:
        await jm_next_page.finish("没有进行中的搜索，请先使用'jm搜索'命令")
        return
    
    logger.debug(f"用户 {user_id} 的搜索状态: {search_state}")
    
    search_query = search_state["query"]
    current_page = search_state["current_page"]
    total_results = search_state["total_results"]
    results_per_page = search_state["results_per_page"]
    
    # 计算下一页的起始和结束索引
    start_idx = (current_page + 1) * results_per_page
    
    # 检查是否需要获取更多结果
    if start_idx >= len(total_results):
        # 尝试获取下一页搜索结果
        next_page_num = current_page + 2  # API页数从1开始，当前页是0
        searching_msg_id = (await jm_next_page.send("搜索更多结果中..."))['message_id']
        
        try:
            next_page = await search_album_async(client, search_query, page=next_page_num)
            await bot.delete_msg(message_id=searching_msg_id)
            
            if next_page and len(next_page) > 0:
                # 有更多结果，添加到总结果中
                next_page_results = list(next_page)
                total_results.extend(next_page_results)
                # 更新搜索状态
                data_manager.save_search_state(user_id, search_query, current_page + 1, total_results)
            else:
                # 没有更多结果了
                await jm_next_page.finish("已经是最后一页了")
                return
        except Exception as e:
            await bot.delete_msg(message_id=searching_msg_id)
            logger.error(f"获取下一页搜索结果失败: {e}")
            await jm_next_page.finish("获取更多结果失败")
            return
    
    # 获取当前页的结果
    end_idx = min(start_idx + results_per_page, len(total_results))
    current_results = total_results[start_idx:end_idx]
    
    # 构建消息
    messages = []
    blocked_message = plugin_config.jmcomic_blocked_message
    
    # 获取详细信息和头像
    album_details = await asyncio.gather(*(get_photo_info_async(client, album_id) for album_id, _ in current_results))
    avatars = await asyncio.gather(*(download_avatar(album_id) for album_id, _ in current_results))
    
    for (album_id, title), photo, avatar in zip(current_results, album_details, avatars):
        # 检查标签是否应该被屏蔽 - 改用data_manager.has_restricted_tag
        if photo and hasattr(photo, 'tags') and photo.tags:
            # 使用data_manager.has_restricted_tag检查
            if data_manager.has_restricted_tag(photo.tags):
                # 添加屏蔽提示到转发消息中
                message = Message(blocked_message)
                message_node = MessageSegment("node", {"name": "jm搜索结果", "content": message})
                messages.append(message_node)
                continue  # 处理下一个结果
        
        # 构建包含详细信息的消息
        message = Message()
        message += f"jm{album_id} | {title}\n"
        
        # 添加作者信息（如果有）
        if photo and hasattr(photo, 'author') and photo.author:
            message += f"👤 作者: {photo.author}\n"
        
        # 添加分类信息（如果有）
        if photo and hasattr(photo, 'category') and photo.category:
            category_info = f"📂 分类: {photo.category.title if hasattr(photo.category, 'title') else '未分类'}"
            if hasattr(photo, 'category_sub') and photo.category_sub and hasattr(photo.category_sub, 'title'):
                if photo.category_sub.title and photo.category_sub.title != photo.category.title:
                    category_info += f" > {photo.category_sub.title}"
            message += category_info + "\n"
        
        # 添加标签信息（如果有）
        if photo and hasattr(photo, 'tags') and photo.tags:
            tag_lines = []
            current_line = "🏷️ 标签: "
            tag_count = 0
            
            for tag in photo.tags:
                if tag_count > 0 and tag_count % 4 == 0:
                    tag_lines.append(current_line)
                    current_line = "          "
                current_line += f"#{tag} "
                tag_count += 1
                
            if current_line != "          ":
                tag_lines.append(current_line)
                
            message += "\n".join(tag_lines)

        # 添加封面图片
        if avatar:
            avatar = await blur_image_async(avatar)
            message += MessageSegment.image(avatar)

        message_node = MessageSegment("node", {"name": "jm搜索结果", "content": message})
        messages.append(message_node)
    
    try:
        await send_forward_message(bot, event, messages)
        
        # 更新搜索状态，增加页码
        data_manager.save_search_state(user_id, search_query, current_page + 1, total_results)
        
        # 检查是否还有更多结果
        has_more = (end_idx < len(total_results)) or (end_idx % results_per_page == 0)
        
        if has_more:
            await jm_next_page.finish(f"输入\"jm下一页\"查看更多结果")
        else:
            await jm_next_page.finish(f"已显示所有搜索结果")
    except ActionFailed:
        await jm_next_page.finish("搜索结果发送失败", reply_message=True)


jm_set_folder = on_command("jm设置文件夹", aliases={"JM设置文件夹"}, permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER, block=True)
@jm_set_folder.handle()
async def _( bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    folder_name = arg.extract_plain_text().strip()
    if not folder_name:
        await jm_set_folder.finish("请输入要设置的文件夹名称")

    group_id = event.group_id

    found_folder_id = None

    try:
        root_data = await bot.call_api("get_group_root_files", group_id=group_id)
        for folder_item in root_data.get("folders", []):
            if folder_item.get("folder_name") == folder_name:
                found_folder_id = folder_item.get("folder_id")
                break
    except ActionFailed as e:
        logger.warning(f"获取群根目录文件夹信息失败：{e}")

    if found_folder_id:
        data_manager.set_group_folder_id(group_id, found_folder_id)
        await jm_set_folder.finish(f"已设置本子储存文件夹")
    else:
        try:
            create_result = await bot.call_api(
                "create_group_file_folder",
                group_id=group_id,
                folder_name=folder_name
            )

            ret_code = create_result["result"]["retCode"]
            if ret_code != 0:
                await jm_set_folder.finish("未找到该文件夹,创建文件夹失败")

            folder_id = create_result["groupItem"]["folderInfo"]["folderId"]
            data_manager.set_group_folder_id(group_id, folder_id)
            await jm_set_folder.finish(f"已设置本子储存文件夹")

        except ActionFailed as e:
            logger.warning("创建文件夹失败")
            await jm_set_folder.finish("未找到该文件夹,主动创建文件夹失败")

# endregion

# region jm成员黑名单指令
jm_ban_user = on_command("jm拉黑", aliases={"JM拉黑"}, permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER, block=True)
@jm_ban_user.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    """将用户加入当前群的黑名单"""
    at_segment = arg[0]
    if at_segment.type != "at":
        await jm_unban_user.finish("请使用@指定要拉黑的用户")

    user_id = at_segment.data["qq"]

    user_id = int(user_id)
    group_id = event.group_id
    operator_id = event.user_id

    if user_id == operator_id:
        await jm_ban_user.finish("你拉黑你自己？")

    has_permission = await check_permission(bot, group_id, operator_id, user_id)
    if not has_permission:
        await jm_unban_user.finish("权限不足")

    data_manager.add_blacklist(group_id, user_id)
    await jm_ban_user.finish(MessageSegment.at(user_id) + f"已加入本群jm黑名单")


jm_unban_user = on_command("jm解除拉黑", aliases={"JM解除拉黑"}, permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER, block=True)
@jm_unban_user.handle()
async def handle_jm_unban_user(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    """将用户移出当前群的黑名单"""
    at_segment = arg[0]
    if at_segment.type != "at":
        await jm_unban_user.finish("请使用@指定要解除拉黑的用户")

    user_id = at_segment.data["qq"]

    user_id = int(user_id)
    group_id = event.group_id
    operator_id = event.user_id

    if user_id == operator_id:
        await jm_ban_user.finish("想都别想！")

    has_permission = await check_permission(bot, group_id, operator_id, user_id)
    if not has_permission:
        await jm_unban_user.finish("权限不足")

    data_manager.remove_blacklist(group_id, user_id)
    await jm_unban_user.finish(MessageSegment.at(user_id) + f"已从本群jm黑名单中移除")


jm_blacklist = on_command( "jm黑名单", aliases={"JM黑名单"}, permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER, block=True)
@jm_blacklist.handle()
async def handle_jm_list_blacklist(bot: Bot, event: GroupMessageEvent):
    """列出当前群的黑名单列表"""
    group_id = event.group_id
    blacklist = data_manager.list_blacklist(group_id)

    if not blacklist:
        await jm_blacklist.finish("当前群的黑名单列表为空")

    msg = "当前群的黑名单列表：\n"
    for user_id in blacklist:
        msg += MessageSegment.at(user_id)

    await jm_blacklist.finish(msg)

# endregion

# region 群功能开关指令
jm_enable_group = on_command("jm启用群", permission=SUPERUSER, block=True)
@jm_enable_group.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """ 启用指定群号，可用空格隔开多个群 """
    raw_text = arg.extract_plain_text().strip()

    group_ids = raw_text.split()
    success_list = []

    for group_id_str in group_ids:
        if not group_id_str.isdigit():
            continue

        group_id = int(group_id_str)
        data_manager.set_group_enabled(group_id, True)
        success_list.append(group_id_str)

    msg = ""
    if success_list:
        msg += "以下群已启用插件功能：\n" + " ".join(success_list)

    await jm_enable_group.finish(msg.strip() or "没有做任何处理。")


jm_disable_group = on_command("jm禁用群", permission=SUPERUSER, block=True)
@jm_disable_group.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    """ 禁用指定群号，可用空格隔开多个群 """
    raw_text = arg.extract_plain_text().strip()

    group_ids = raw_text.split()
    success_list = []

    for group_id_str in group_ids:
        if not group_id_str.isdigit():
            continue

        group_id = int(group_id_str)
        data_manager.set_group_enabled(group_id, False)
        success_list.append(group_id_str)

    msg = ""
    if success_list:
        msg += "以下群已禁用插件功能：\n" + " ".join(success_list)

    await jm_disable_group.finish(msg.strip() or "没有做任何处理。")

jm_enable_here = on_command("开启jm", aliases={"开启JM"}, permission=SUPERUSER, block=True)
@jm_enable_here.handle()
async def handle_jm_enable_here(event: GroupMessageEvent):
    group_id = event.group_id
    data_manager.set_group_enabled(group_id, True)
    await jm_enable_here.finish("已启用本群jm功能！")


jm_disable_here = on_command("关闭jm", aliases={"关闭JM"}, permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER, block=True)
@jm_disable_here.got("confirm", prompt="禁用后只能请求神秘存在再次开启该功能！确认要关闭吗？发送'确认'关闭")
async def _(event: GroupMessageEvent, confirm: str = ArgPlainText()):
    if confirm == "确认":
        group_id = event.group_id
        data_manager.set_group_enabled(group_id, False)
        await jm_disable_here.finish("已禁用本群jm功能！")

# endregion

# region 添加屏蔽tags和jm号
jm_forbid_id = on_command("jm禁用id", aliases={"JM禁用id"}, permission=SUPERUSER, block=True)
@jm_forbid_id.handle()
async def handle_jm_forbid_id(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    raw_text = arg.extract_plain_text().strip()

    jm_ids = raw_text.split()
    success_list = []

    for jm_id in jm_ids:
        if not jm_id.isdigit():
            continue
        data_manager.add_restricted_jm_id(jm_id)
        success_list.append(jm_id)

    msg = ""
    if success_list:
        msg += "以下jm号已加入禁止下载列表：\n" + " ".join(success_list)

    await jm_forbid_id.finish(msg.strip() or "没有做任何处理")


jm_forbid_tag = on_command("jm禁用tag", aliases={"JM禁用tag"}, permission=SUPERUSER, block=True)
@jm_forbid_tag.handle()
async def handle_jm_forbid_tag(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    raw_text = arg.extract_plain_text().strip()

    tags = raw_text.split()
    success_list = []

    for tag in tags:
        if not tag:
            continue
        data_manager.add_restricted_tag(tag)
        success_list.append(tag)

    msg = ""
    if success_list:
        msg += "以下tag已加入禁止下载列表：\n" + " ".join(success_list)

    await jm_forbid_tag.finish(msg.strip() or "没有做任何处理")

# endregion

@scheduler.scheduled_job("cron", day_of_week="mon", hour=0, minute=0, id="reset_user_limits")
async def reset_user_limits():
    """ 每周一凌晨0点重置所有用户的下载次数 """
    try:
        user_limits = data_manager.data.get("user_limits", {})

        if not user_limits:
            logger.info("无用户下载数据可供重置。")
            return

        for user_id in user_limits.keys():
            data_manager.set_user_limit(int(user_id), plugin_config.jmcomic_user_limits)

        logger.info("所有用户的下载次数已成功刷新")

    except Exception as e:
        logger.error(f"刷新用户下载次数时出错：{e}")


@scheduler.scheduled_job("cron", hour=3, minute=0)
async def clear_cache_dir():
    """ 每天凌晨3点清理缓存文件夹 """
    try:
        if plugin_cache_dir.exists():
            shutil.rmtree(plugin_cache_dir)
            plugin_cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"已成功清理缓存目录：{cache_dir}")
    except Exception as e:
        logger.error(f"清理缓存目录失败：{e}")