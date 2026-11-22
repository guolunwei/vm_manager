"""模型公共基类与跨版本兼容工具。"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


def check_constraint(*, condition: models.Q, name: str) -> models.CheckConstraint:
    """数据库层 CHECK 约束。

    Django 6 把 ``CheckConstraint(check=...)`` 改名为 ``condition=``，
    这里做个兼容，让项目同时能跑在 5.2 与 6.x 上。
    """
    try:
        return models.CheckConstraint(condition=condition, name=name)
    except TypeError:  # Django < 6
        return models.CheckConstraint(check=condition, name=name)
