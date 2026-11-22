# 在 Windows 上把 VM Manager 装成开机自启服务

Windows 没有 systemd，最省事的办法是用 [NSSM](https://nssm.cc/download)
（Non-Sucking Service Manager）把两个进程包成服务。

## 1. 准备

```bat
cd D:\Applications\vm_manager

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py bootstrap
```

先把 `config.ini` 按实际环境改好，然后手工验证一遍能跑：

```bat
.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

另开一个窗口：

```bat
.venv\Scripts\python.exe manage.py worker
```

确认页面能开、能创建虚拟机之后，再装服务。

## 2. 装两个服务

以**管理员**身份打开 cmd，把 `nssm.exe` 放到 PATH 里或写全路径。

### Web

```bat
nssm install VmManagerWeb "D:\Applications\vm_manager\.venv\Scripts\python.exe" ^
    "-m" "waitress" "--listen=0.0.0.0:8000" "config.wsgi:application"
nssm set VmManagerWeb AppDirectory "D:\Applications\vm_manager"
nssm set VmManagerWeb AppEnvironmentExtra DJANGO_SETTINGS_MODULE=config.settings.prod
nssm set VmManagerWeb AppStdout "D:\Applications\vm_manager\data\web.out.log"
nssm set VmManagerWeb AppStderr "D:\Applications\vm_manager\data\web.err.log"
nssm set VmManagerWeb AppRotateFiles 1
nssm set VmManagerWeb AppRotateBytes 8388608
nssm start VmManagerWeb
```

### Worker

```bat
nssm install VmManagerWorker "D:\Applications\vm_manager\.venv\Scripts\python.exe" ^
    "manage.py" "worker"
nssm set VmManagerWorker AppDirectory "D:\Applications\vm_manager"
nssm set VmManagerWorker AppEnvironmentExtra DJANGO_SETTINGS_MODULE=config.settings.prod
nssm set VmManagerWorker AppStdout "D:\Applications\vm_manager\data\worker.out.log"
nssm set VmManagerWorker AppStderr "D:\Applications\vm_manager\data\worker.err.log"
nssm set VmManagerWorker AppRotateFiles 1
nssm set VmManagerWorker AppRotateBytes 8388608
nssm set VmManagerWorker AppExit Default Restart
nssm start VmManagerWorker
```

## 3. 关于运行账号

服务默认以 `LocalSystem` 运行。这带来两个影响：

1. **`~/.ssh` 是 `C:\Windows\System32\config\systemprofile\.ssh`**，
   而不是你自己的 `C:\Users\你\.ssh`。想在创建时自动下发公钥，请在
   `config.ini` 里显式配置：

   ```ini
   export SSH_PUBKEY="ssh-ed25519 AAAA... you@host"
   ```

2. **对虚拟机目录的权限**要够。如果 `BASE_DIR` 在某个用户目录下，
   给 `LocalSystem`（或改成一个专用账号）加读写权限。

想用特定账号运行：

```bat
nssm set VmManagerWeb ObjectName ".\vmuser" "密码"
nssm set VmManagerWorker ObjectName ".\vmuser" "密码"
```

## 4. 常用命令

```bat
nssm status  VmManagerWeb
nssm restart VmManagerWeb
nssm stop    VmManagerWorker
nssm remove  VmManagerWeb confirm
```

## 5. 卸载

```bat
nssm stop   VmManagerWeb    & nssm remove VmManagerWeb    confirm
nssm stop   VmManagerWorker & nssm remove VmManagerWorker confirm
```

## 备选：任务计划程序

不想装 NSSM 的话，「任务计划程序」也能凑合：建两个任务，触发器选「计算机启动时」，
操作分别指向上面两条命令行，勾上「不管用户是否登录都要运行」。

缺点是崩溃不会自动重启、日志要自己重定向，所以只建议临时用。
