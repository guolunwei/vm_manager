### Virtual Machine Management Script
This script provides functionality to manage virtual machines (VMs) based on a template snapshot. 
It supports listing, cloning, removing, starting, stopping, restarting and setting the IP address of VMs.

### Prerequisites
- Install Git for Windows.
- Install VMware Workstation and and put vmrun.exe in Git's PATH.
- Create a vm, take a snapshot of it, then modify the config.ini configuration.

### Installation
```bash
git clone https://github.com/guolunwei/vm_manager.git
chmod -R +x vm_manager/*.sh

cd vm_manager
./set_env.sh

source /etc/profile

# Modify configurations
vim /path/to/vm_manager/config.ini
```

### Usage
```bash
Usage: vm [command] [arguments]

Available commands:
  list       List running or all VMs     vm list [--all]
  clone      Clone one or more VMs       vm clone <vm_name> [...]
  remove     Remove one or more VMs      vm remove <vm_name> [...]
  start      Start one or more VMs       vm start <vm_name> [...]
  stop       Stop one or more VMs        vm stop <vm_name> [...]
  restart    Restart one or more VMs     vm restart <vm_name> [...]
  setip      Set IP address of a VM      vm setip <vm_name> <ip_addr>
  ssh        SSH into a VM               vm ssh <vm_name>

For example:
  vm clone web1 web2
  vm setip web1 192.168.88.100
```
