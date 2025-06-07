# JupyterHub 配置文件
import os
from jupyterhub.auth import Authenticator
# 基本配置
c = get_config()
# ================
# 基础配置
# ================
c.JupyterHub.ip = '0.0.0.0'
c.JupyterHub.port = 8000

# Hub服务地址
c.JupyterHub.hub_bind_url = 'http://127.0.0.1:8082'

# ================
# Spawner配置 (单用户进程孵化器)
# ================
c.Spawner.default_url = '/lab'  # 默认启动JupyterLab
c.Spawner.notebook_dir = '~'    # 笔记本目录设置为用户主目录
c.Spawner.args = ['--allow-root']  # 允许root权限

# ================
# HTTP代理配置
# ================
c.ConfigurableHTTPProxy.api_url = 'http://localhost:8001'  # hub与http代理通信的API端点
c.ConfigurableHTTPProxy.should_start = True  # 允许hub启动代理(默认为False则需要手动启动)

# ================
# 用户验证配置 (Authenticator)
# ================
c.JupyterHub.authenticator_class = 'jupyterhub.auth.CustomAuthenticator'  # 采用自定义身份验证
c.Authenticator.allow_existing_users = True  # 允许通过JupyterHub API或管理页面/hub/admin管理用户
c.LocalAuthenticator.create_system_users=True # 允许创建系统用户automatically create system users from jupyterhub users
"""
当添加用户时：
- 如果allow_existing_users为True，该用户将自动添加到allowed_users集和数据库中，重启hub将保留
- 如果allow_existing_users为False，则不允许未通过配置(如allowed_users)授予访问权限的用户登录
"""

c.Authenticator.admin_users = {'admin', 'z','root'}  # 管理员用户
c.Authenticator.allow_all = True  # 允许所有通过身份验证的人访问jupyterhub
c.Authenticator.allowed_users = set()  # 允许部分通过身份验证的人访问jupyterhub(当allow_all=False时生效)
c.Authenticator.delete_invalid_users = True  # 从jupyterhub.sqlite用户数据库中自动删除无效用户

# ================
# Spawner配置 (单用户进程孵化器)
# ================
c.JupyterHub.spawner_class = 'jupyterhub.spawner.CustomSpawner'  # 单用户jupyter服务生成器

# ================
# 空闲服务管理
# ================
# 单用户jupyter进程关闭服务，默认3600s后kill，减少资源浪费
c.JupyterHub.services = [
    {
        'name': 'idle-culler',
        'command': ['python3', '-m', 'jupyterhub_idle_culler', '--timeout=3600'],
    }
]

c.JupyterHub.load_roles = [
    {
        "name": "list-and-cull",  # 角色名称
        "services": ["idle-culler"],  # 将服务分配给此角色
        "scopes": [
            "list:users",            # 列出用户
            "read:users:activity",    # 读取用户最后活动时间
            "admin:servers",          # 启动/停止服务器
        ],
    }
]

# ================
# 文件路径配置
# ================
c.JupyterHub.cookie_secret_file = '/opt/jupyterhub/jupyterhub_cookie_secret'
c.JupyterHub.db_url = '/opt/jupyterhub/jupyterhub.sqlite'
c.JupyterHub.pid_file = '/opt/jupyterhub/jupyterhub.pid'

