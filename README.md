### Virtual Machine Management Script
This script provides functionality to manage virtual machines (VMs) based on a template snapshot. It supports listing, cloning, removing, starting, stopping, restarting and setting the IP address of VMs.

### Prerequisites
- vmrun.exe utility must be installed and accessible from the command line.
- The source VMX file (Rocky8_nsd.vmx) should exist at D:\Virtual Machines\Rocky8_nsd\Rocky8_nsd.vmx.
- The directory D:\Virtual Machines should be accessible and writable.
- For windows, cygwin64 or WSL2 is available.

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
