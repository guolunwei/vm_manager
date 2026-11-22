"""持久化后台任务。

页面上所有耗时操作（克隆、开机、改 IP、快照…）都不在请求线程里做，而是：

1. 视图里 :func:`vmmanager.jobs.queue.enqueue` 入队，立刻返回 job id；
2. 常驻进程 ``python manage.py worker`` 取任务执行；
3. 前端轮询 ``/api/jobs/<id>/`` 拉增量日志。

这样刷新页面、关掉浏览器、甚至重启 Web 进程，任务都不会丢。
"""
