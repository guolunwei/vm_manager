"""服务层。

分模块职责：

=====================================  ==========================================
模块                                   职责
=====================================  ==========================================
:mod:`vmmanager.services.vmrun`         vmrun 命令封装（唯一与 VMware 直接对话的层）
:mod:`vmmanager.services.vmx`           .vmx 文件读写
:mod:`vmmanager.services.guest`         客户机内部操作（主机名/IP/公钥/密码）
:mod:`vmmanager.services.network`       IP 探测、网段扫描、地址分配
:mod:`vmmanager.services.provision`     创建编排
:mod:`vmmanager.services.power`         电源与删除
:mod:`vmmanager.services.snapshot`      快照
:mod:`vmmanager.services.inventory`     纳管与巡检
:mod:`vmmanager.services.metrics`       资源指标
=====================================  ==========================================

约定：服务层不认识 HTTP，也不直接返回 JSON；页面与 API 都由各自的上层负责。
"""
