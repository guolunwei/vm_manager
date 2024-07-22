### Virtual Machine Management Script
This script provides functionality to manage virtual machines (VMs) based on a template snapshot. It supports cloning, removing, starting, stopping, and setting the IP address of VMs.

### README.md

- en [English](README.md)
- zh [中文](README.zh_CN.md)

### Prerequisites
- vmrun.exe utility must be installed and accessible from the command line.
- The source VMX file (Rocky8_nsd.vmx) should exist at D:\Virtual Machines\Rocky8_nsd\Rocky8_nsd.vmx.
- The directory D:\Virtual Machines should be accessible and writable.
- For windows, cygwin64 or WSL2 is available.

### Usage
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

### Examples
Clone two VMs named web1 and web2:
```bash
  ./vmctl.sh clone web1 web2
```
Set the IP address of web1 to 192.168.1.100:
```bash
  ./vmctl.sh setip web1 192.168.1.100
```
