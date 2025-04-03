from pathlib import Path
from typing import List

from nonebot import get_plugin_config, require
from pydantic import BaseModel, Field

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_dir


class Config(BaseModel):
    jmcomic_log: bool = Field(default=False, description="是否启用JMComic API日志")
    jmcomic_proxies: str = Field(default="system", description="代理配置")
    jmcomic_thread_count: int = Field(default=10, description="下载线程数量")
    jmcomic_username: str = Field(description="JM登录用户名")
    jmcomic_password: str = Field(description="JM登录密码")
    jmcomic_allow_groups: bool = Field(default=False, description="是否默认启用所有群")
    jmcomic_user_limits: int = Field(default=5, description="每位用户的每周下载限制次数")
    jmcomic_modify_real_md5: bool = Field(default=False, description="是否真正修改PDF文件的MD5值")
    jmcomic_blocked_keywords: List[str] = Field(default=[], description="搜索屏蔽词列表")
    jmcomic_blocked_tags: List[str] = Field(default=[], description="搜索标签屏蔽列表")
    jmcomic_blocked_message: str = Field(default="猫猫吃掉了一个不豪吃的本子", description="搜索屏蔽时显示的消息")

plugin_config = get_plugin_config(Config)

plugin_cache_dir: Path = get_plugin_cache_dir()
cache_dir = plugin_cache_dir.as_posix()

username = plugin_config.jmcomic_username
password = plugin_config.jmcomic_password

# 处理带方括号的字符串情况
if isinstance(username, str) and username.startswith('[') and username.endswith(']'):
    username = username.strip('[]').strip().strip('"\'')
if isinstance(password, str) and password.startswith('[') and password.endswith(']'):
    password = password.strip('[]').strip().strip('"\'')

# 如果是列表，取第一个元素
if isinstance(username, list) and username:
    username = username[0]
if isinstance(password, list) and password:
    password = password[0]

# 如果是数字，转为字符串
if isinstance(username, int):
    username = str(username)
if isinstance(password, int):
    password = str(password)

config_data = f"""
log: {plugin_config.jmcomic_log}

client:
  impl: api
  retry_times: 1
  postman:
    meta_data:
      proxies: {plugin_config.jmcomic_proxies}

download:
  image:
    suffix: .jpg
  threading:
    image: {plugin_config.jmcomic_thread_count}

dir_rule:
  base_dir: {cache_dir}
  rule: Bd_Pid

plugins:
  after_init:
    - plugin: login
      kwargs:
        username: {username}
        password: {password}

  after_photo:
    - plugin: img2pdf
      kwargs:
        pdf_dir: {cache_dir}
        filename_rule: Pid
"""