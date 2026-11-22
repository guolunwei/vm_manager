"""页面模板的小工具过滤器。

只放「展示逻辑」：把数值映射成视觉等级这类纯函数。任何需要查库或调
vmrun 的逻辑都不该出现在这里——那是视图的职责。
"""

from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag
def static_ver(rel_path: str) -> str:
    """静态资源的版本号（文件 mtime），用于 ?v= 缓存失效。

    文件不存在时返回 0——总比每次都命中旧缓存强。
    """
    from pathlib import Path

    base = Path(settings.BASE_DIR) / "static"
    try:
        return str(int((base / rel_path).stat().st_mtime))
    except OSError:
        return "0"


@register.filter
def level_class(value) -> str:
    """把百分比映射成 meter 进度条的等级色：none/ok/warn/danger。

    None（拿不到数据）和 0 归为 none；80 以上算危险，60 以上算警告。
    """
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return "none"
    if percent <= 0:
        return "none"
    if percent >= 80:
        return "danger"
    if percent >= 60:
        return "warn"
    return "ok"
