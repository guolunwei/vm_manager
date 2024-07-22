### Virtual Machine Management Script
This script provides functionality to manage virtual machines (VMs) based on a template snapshot. It supports cloning, removing, starting, stopping, and setting the IP address of VMs.

### README.md

- en [English](README.md)
- zh [中文](README.zh_CN.md)

### 前提条件
- vmrun.exe 已经安装并可被命令行访问，需要添加路径到环境变量中
- SRC_VMX 是源虚拟机的位置，SNAPSHOT 是源虚拟机的快照位置
- BASE_DIR 是目标虚拟机保存的目录
- 对于windows，cygwin64 或者 WSL2 可用

### 使用方法
```bash
./vmctl.sh [command] [arguments]

Available Commands
clone: Clone one or more VMs from the template snapshot.
      ./vmctl.sh clone <vm_name> [...]
remove: Remove one or more VMs.
      ./vmctle.sh remove <vm_name> [...]
start: Start one or more VMs.
      ./vmctle.sh start <vm_name> [...]
stop: Stop one or more VMs.
      ./vmctle.sh stop <vm_name> [...]
setip: Set the IP address of a VM.
      ./vmctle.sh setip <vm_name> <ip_addr>
```

### 示例
克隆名称web1 和web2 的虚拟机:
```bash
  ./vmctl.sh clone web1 web2
```
给web1 虚拟机设置IP 为 192.168.1.100:
```bash
  ./vmctl.sh setip web1 192.168.1.100
```
