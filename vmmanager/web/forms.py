"""表单。

视图里只做「校验 + 入队」，真正的业务在 :mod:`vmmanager.jobs.handlers`。
所以表单的职责很纯粹：把用户输入整理成 payload。
"""

from __future__ import annotations

import ipaddress

from django import forms

from vmmanager.models import CloneType, Flavor, IpLease, VmTemplate

_INPUT = {"class": "input"}
_TEXTAREA = {"class": "input", "rows": 3}


class VmCreateForm(forms.Form):
    name = forms.CharField(
        label="名称",
        max_length=64,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "如 web-01"}),
    )
    template = forms.ModelChoiceField(
        label="模板",
        queryset=VmTemplate.objects.none(),
        widget=forms.Select(attrs=_INPUT),
    )
    flavor = forms.ModelChoiceField(
        label="规格",
        queryset=Flavor.objects.none(),
        required=False,
        widget=forms.Select(attrs=_INPUT),
        help_text="选择预设规格，或在下方自定义",
    )
    cpus = forms.IntegerField(
        label="vCPU",
        min_value=1,
        max_value=128,
        required=False,
        widget=forms.NumberInput(attrs=_INPUT),
    )
    memory_mb = forms.IntegerField(
        label="内存（MB）",
        min_value=256,
        max_value=1048576,
        required=False,
        step_size=256,
        widget=forms.NumberInput(attrs={**_INPUT, "step": 256}),
    )
    ip = forms.CharField(
        label="IP 地址",
        required=False,
        max_length=45,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "留空则自动分配"}),
    )
    clone_type = forms.ChoiceField(
        label="克隆方式",
        choices=CloneType.choices,
        initial=CloneType.LINKED,
        widget=forms.Select(attrs=_INPUT),
    )
    deploy_ssh_key = forms.BooleanField(label="下发本机 SSH 公钥", required=False, initial=True)
    guest_password = forms.CharField(
        label="客户机密码",
        required=False,
        max_length=128,
        widget=forms.PasswordInput(
            attrs={**_INPUT, "placeholder": "留空则使用模板密码", "autocomplete": "new-password"}
        ),
    )
    notes = forms.CharField(
        label="备注",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs=_TEXTAREA),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        templates = VmTemplate.objects.filter(is_active=True)
        self.fields["template"].queryset = templates
        self.fields["flavor"].queryset = Flavor.objects.filter(is_active=True)

        first = templates.first()
        if first is not None:
            self.fields["template"].initial = first.pk
            if first.default_flavor_id:
                self.fields["flavor"].initial = first.default_flavor_id
            self.fields["cpus"].initial = first.default_cpus
            self.fields["memory_mb"].initial = first.default_memory_mb

    def clean_name(self) -> str:
        return self.cleaned_data["name"].strip()

    def clean_ip(self) -> str:
        raw = (self.cleaned_data.get("ip") or "").strip()
        if not raw:
            return ""
        try:
            ipaddress.ip_address(raw)
        except ValueError as exc:
            raise forms.ValidationError("IP 地址格式无效") from exc
        if IpLease.objects.filter(ip=raw).exists():
            raise forms.ValidationError("该 IP 已被占用，请换一个或留空自动分配")
        return raw

    def clean(self):
        data = super().clean()
        flavor = data.get("flavor")
        # 没选规格就用模板默认
        if not data.get("cpus"):
            template = data.get("template")
            data["cpus"] = flavor.cpus if flavor else (template.default_cpus if template else 2)
        if not data.get("memory_mb"):
            template = data.get("template")
            data["memory_mb"] = (
                flavor.memory_mb if flavor else (template.default_memory_mb if template else 4096)
            )
        return data

    def to_payload(self) -> dict:
        data = self.cleaned_data
        return {
            "name": data["name"],
            "template_id": data["template"].pk,
            "flavor_id": data["flavor"].pk if data.get("flavor") else None,
            "cpus": data["cpus"],
            "memory_mb": data["memory_mb"],
            "ip": data.get("ip") or "",
            "clone_type": data["clone_type"],
            "deploy_ssh_key": bool(data.get("deploy_ssh_key")),
            "guest_password": data.get("guest_password") or "",
            "notes": data.get("notes") or "",
        }


class SetIpForm(forms.Form):
    ip = forms.GenericIPAddressField(
        # placeholder 在视图里按模板网段动态替换，这里只是兜底
        label="新 IP",
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "如 192.168.1.10"}),
    )
    prefix = forms.IntegerField(
        label="子网前缀",
        min_value=1,
        max_value=32,
        initial=24,
        required=False,
        widget=forms.NumberInput(attrs=_INPUT),
    )
    gateway = forms.CharField(
        label="网关",
        required=False,
        max_length=45,
        widget=forms.TextInput(attrs=_INPUT),
    )
    dns = forms.CharField(
        label="DNS",
        required=False,
        max_length=64,
        widget=forms.TextInput(attrs=_INPUT),
    )
    guest_password = forms.CharField(
        label="客户机密码",
        required=False,
        max_length=128,
        widget=forms.PasswordInput(
            attrs={**_INPUT, "placeholder": "留空则使用模板密码", "autocomplete": "new-password"}
        ),
    )


class ResizeForm(forms.Form):
    """调整已有虚拟机的规格。运行中的机器会由任务先关机再开机。"""

    cpus = forms.IntegerField(
        label="vCPU",
        min_value=1,
        max_value=128,
        widget=forms.NumberInput(attrs=_INPUT),
    )
    memory_mb = forms.IntegerField(
        label="内存（MB）",
        min_value=256,
        max_value=1048576,
        widget=forms.NumberInput(attrs={**_INPUT, "step": 256}),
    )


class CloneForm(forms.Form):
    """把一台已有虚拟机克隆成一台新机器（完整克隆，带磁盘内容）。"""

    name = forms.CharField(
        label="新机器名称",
        max_length=64,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "如 web-02"}),
    )
    ip = forms.CharField(
        label="IP 地址",
        required=False,
        max_length=45,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "留空则自动分配"}),
    )

    def clean_name(self) -> str:
        return self.cleaned_data["name"].strip()

    def clean_ip(self) -> str:
        raw = (self.cleaned_data.get("ip") or "").strip()
        if not raw:
            return ""
        try:
            ipaddress.ip_address(raw)
        except ValueError as exc:
            raise forms.ValidationError("IP 地址格式无效") from exc
        if IpLease.objects.filter(ip=raw).exists():
            raise forms.ValidationError("该 IP 已被占用")
        return raw


class GrowDiskForm(forms.Form):
    """磁盘扩容。「新增容量」与「目标容量」二选一。"""

    grow_gb = forms.IntegerField(
        label="新增容量（GB）",
        min_value=0,
        required=False,
        widget=forms.NumberInput(attrs=_INPUT),
    )
    size_gb = forms.IntegerField(
        label="目标容量（GB，绝对值）",
        min_value=0,
        required=False,
        widget=forms.NumberInput(attrs=_INPUT),
    )

    def clean(self):
        data = super().clean()
        grow = data.get("grow_gb") or 0
        size = data.get("size_gb") or 0
        if (grow <= 0 and size <= 0) or (grow > 0 and size > 0):
            raise forms.ValidationError("「新增容量」和「目标容量」必须且只能填一个")
        return data


class TemplateForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ModelForm 的下拉默认空选项是英文，改成中文
        self.fields["default_flavor"].empty_label = "跟随规格选择"
        self.fields["default_flavor"].required = False

    class Meta:
        from vmmanager.models import VmTemplate as Model

        model = Model
        fields = [
            "name",
            "vmx_path",
            "snapshot",
            "os_family",
            "network_mode",
            "guest_user",
            "guest_password",
            "guest_connection",
            "prefix_len",
            "gateway",
            "dns",
            "default_cpus",
            "default_memory_mb",
            "default_flavor",
            "deploy_ssh_key",
            "is_active",
            "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "vmx_path": forms.TextInput(
                attrs={**_INPUT, "placeholder": r"D:\Virtual Machines\CentOS7_cloud\CentOS7_cloud.vmx"}
            ),
            "snapshot": forms.TextInput(attrs={**_INPUT, "placeholder": "init"}),
            "os_family": forms.Select(attrs=_INPUT),
            "network_mode": forms.Select(attrs=_INPUT),
            "guest_user": forms.TextInput(attrs=_INPUT),
            "guest_password": forms.PasswordInput(attrs=_INPUT, render_value=True),
            "guest_connection": forms.TextInput(attrs={**_INPUT, "placeholder": "eth0"}),
            "prefix_len": forms.NumberInput(attrs=_INPUT),
            "gateway": forms.TextInput(attrs=_INPUT),
            "dns": forms.TextInput(attrs=_INPUT),
            "default_cpus": forms.NumberInput(attrs=_INPUT),
            "default_memory_mb": forms.NumberInput(attrs={**_INPUT, "step": 256}),
            "default_flavor": forms.Select(attrs=_INPUT),
            "notes": forms.Textarea(attrs=_TEXTAREA),
        }


class FlavorForm(forms.ModelForm):
    class Meta:
        from vmmanager.models import Flavor as Model

        model = Model
        fields = ["name", "cpus", "memory_mb", "description", "sort_order", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={**_INPUT, "placeholder": "4C8G"}),
            "cpus": forms.NumberInput(attrs=_INPUT),
            "memory_mb": forms.NumberInput(attrs={**_INPUT, "step": 256}),
            "description": forms.TextInput(attrs=_INPUT),
            "sort_order": forms.NumberInput(attrs=_INPUT),
        }


class SnapshotForm(forms.Form):
    name = forms.CharField(
        label="快照名称",
        max_length=128,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "如 before-upgrade"}),
    )
    description = forms.CharField(
        label="描述",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={**_INPUT, "placeholder": "描述（可选）"}),
    )


class LoginForm(forms.Form):
    username = forms.CharField(
        label="用户名",
        widget=forms.TextInput(attrs={**_INPUT, "autofocus": True, "autocomplete": "username"}),
    )
    password = forms.CharField(
        label="密码",
        widget=forms.PasswordInput(attrs={**_INPUT, "autocomplete": "current-password"}),
    )
