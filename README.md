### Virtual Machine Management Script
This script provides functionality to manage virtual machines (VMs) based on a template snapshot. It supports listing, cloning, removing, starting, stopping, restarting and setting the IP address of VMs.

### Prerequisites
- vmrun.exe utility must be installed and accessible from the command line.
- The source VMX file (Rocky8_nsd.vmx) should exist at D:\Virtual Machines\Rocky8_nsd\Rocky8_nsd.vmx.
- The directory D:\Virtual Machines should be accessible and writable.
- For windows, cygwin64 or WSL2 is available.

### Usage
```bash
Usage: /d/Downloads/repos/vm_manager/vmctl.sh [command] [arguments]

Available commands:
  list       List running or all VMs     /d/Downloads/repos/vm_manager/vmctl.sh list [--all]
  clone      Clone one or more VMs       /d/Downloads/repos/vm_manager/vmctl.sh clone <vm_name> [...]
  remove     Remove one or more VMs      /d/Downloads/repos/vm_manager/vmctl.sh remove <vm_name> [...]
  start      Start one or more VMs       /d/Downloads/repos/vm_manager/vmctl.sh start <vm_name> [...]
  stop       Stop one or more VMs        /d/Downloads/repos/vm_manager/vmctl.sh stop <vm_name> [...]
  restart    Restart one or more VMs     /d/Downloads/repos/vm_manager/vmctl.sh restart <vm_name> [...]
  setip      Set IP address of a VM      /d/Downloads/repos/vm_manager/vmctl.sh setip <vm_name> <ip_addr>
  ssh        SSH into a VM               /d/Downloads/repos/vm_manager/vmctl.sh ssh <vm_name>

For example:
  /d/Downloads/repos/vm_manager/vmctl.sh clone web1 web2
  /d/Downloads/repos/vm_manager/vmctl.sh setip web1 192.168.88.100
```
