/* ==========================================================================
   VM Manager — 前端交互
   无框架、无构建。所有耗时操作都是「提交后拿到 job id，再轮询日志」。
   ========================================================================== */
(function () {
  "use strict";

  // ---------------------------------------------------------------- 基础工具
  function csrfToken() {
    const input = document.querySelector("#csrf-holder input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function request(url, options = {}) {
    // 注意顺序：先展开 options 再拼 headers，否则 post() 传的 headers
    // 会把整个 headers 对象覆盖掉，X-CSRFToken 就丢了（CSRF Failed: token missing）。
    const opts = {
      credentials: "same-origin",
      ...options,
      headers: { "X-CSRFToken": csrfToken(), "Accept": "application/json", ...(options.headers || {}) },
    };

    const response = await fetch(url, opts);
    const raw = await response.text();
    let data = raw;
    try { data = raw ? JSON.parse(raw) : {}; } catch (_) { /* 非 JSON，保留文本 */ }

    if (!response.ok) {
      const message =
        (data && (data.error || data.detail)) ||
        (typeof data === "string" && data.slice(0, 200)) ||
        `请求失败 (HTTP ${response.status})`;
      throw new Error(message);
    }
    return data;
  }

  const get = (url) => request(url, { method: "GET" });
  const post = (url, body) => request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
    body: JSON.stringify(body || {}),
  });

  // ------------------------------------------------------------------ Toast
  function toast(message, type = "info", duration = 4200) {
    const root = document.getElementById("toast-root");
    if (!root) return;
    const node = document.createElement("div");
    node.className = `toast ${type}`;
    node.textContent = message;
    root.appendChild(node);
    setTimeout(() => {
      node.style.transition = "opacity .2s ease";
      node.style.opacity = "0";
      setTimeout(() => node.remove(), 220);
    }, duration);
  }

  // -------------------------------------------------------------- 全局事件
  function setBusy(button, busy, label) {
    if (busy) {
      button.dataset.originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = label || "处理中…";
    } else {
      button.disabled = false;
      button.textContent = button.dataset.originalLabel || button.textContent;
    }
  }

  async function followJob(jobId) {
    if (jobId) {
      window.location.href = `/jobs/${jobId}/`;
      return;
    }
    window.location.reload();
  }

  // 入队成功后的跳转策略：
  // 前面还有任务在排队时**不跳走**——用户很可能要连着点好几个动作，
  // 直接跳到任务详情页会把后续操作打断。留在列表页刷新一下即可。
  async function afterEnqueue(data) {
    const ahead = Number(data && data.queued_behind) || 0;
    if (ahead > 0) {
      toast(`已提交，排在 ${ahead} 个任务之后`, "ok", 3000);
      setTimeout(() => window.location.reload(), 700);
      return;
    }
    await followJob(data && data.job_id);
  }

  document.addEventListener("click", async (event) => {
    // 复制
    const copyBtn = event.target.closest("[data-copy]");
    if (copyBtn) {
      const text = copyBtn.dataset.copy;
      try {
        await navigator.clipboard.writeText(text);
        toast("已复制", "ok", 1600);
      } catch (_) {
        toast("复制失败，请手动选中文字复制", "err");
      }
      return;
    }

    const copyTarget = event.target.closest("[data-copy-target]");
    if (copyTarget) {
      const node = document.getElementById(copyTarget.dataset.copyTarget);
      if (node) {
        try {
          await navigator.clipboard.writeText(node.textContent);
          toast("已复制", "ok", 1600);
        } catch (_) {
          toast("复制失败，请手动选中文字复制", "err");
        }
      }
      return;
    }

    // 同步磁盘
    const syncBtn = event.target.closest('[data-action="sync"]');
    if (syncBtn) {
      event.preventDefault();
      setBusy(syncBtn, true, "同步中…");
      try {
        const data = await post("/api/v1/sync/", {});
        toast("磁盘同步任务已提交", "ok");
        await followJob(data.job_id);
      } catch (err) {
        toast(err.message, "err");
        setBusy(syncBtn, false);
      }
      return;
    }

    // 通用 VM 动作（电源 / 探测 / 回滚快照 / 重试配置）
    const actionBtn = event.target.closest('[data-action="vm-action"]');
    if (actionBtn) {
      event.preventDefault();
      const name = actionBtn.dataset.name || "";
      const label = actionBtn.dataset.op || "";
      if (label === "stop" && !confirm(`确定关机 ${name}？`)) return;
      if (label === "reset" && !confirm(`确定重启 ${name}？`)) return;
      if (label === "revert" && !confirm(`确定回滚到快照 ${name}？当前状态将丢失。`)) return;

      setBusy(actionBtn, true, "处理中…");
      try {
        const data = await post(actionBtn.dataset.url, {});
        await afterEnqueue(data);
      } catch (err) {
        toast(err.message, "err");
        setBusy(actionBtn, false);
      }
      return;
    }

    // 批量操作（仪表盘勾选行）
    const bulkBtn = event.target.closest("[data-bulk]");
    if (bulkBtn) {
      event.preventDefault();
      submitBulk(bulkBtn);
      return;
    }

    // 删除
    const deleteBtn = event.target.closest('[data-action="vm-delete"]');
    if (deleteBtn) {
      event.preventDefault();
      const name = deleteBtn.dataset.name || "";
      const typed = prompt(
        `将删除 VM ${name} 及其磁盘文件，此操作不可撤销。\n请输入机器名称以确认：`
      );
      if (typed === null) return;
      if (typed.trim() !== name) {
        toast("名称不一致，已取消", "err");
        return;
      }
      setBusy(deleteBtn, true, "删除中…");
      try {
        const data = await post(deleteBtn.dataset.url, { delete_files: "1" });
        await afterEnqueue(data);
      } catch (err) {
        toast(err.message, "err");
        setBusy(deleteBtn, false);
      }
    }
  });

  // 表单异步提交（返回 job_id 的那些）
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest('form[data-action="submit-job"]');
    if (!form) return;
    event.preventDefault();

    const button = form.querySelector('button[type="submit"]');
    setBusy(button, true, "提交中…");
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      const raw = await response.text();
      let data = {};
      try { data = JSON.parse(raw); } catch (_) { data = {}; }

      if (!response.ok) {
        throw new Error(data.error || `提交失败 (HTTP ${response.status})`);
      }
      await afterEnqueue(data);
    } catch (err) {
      toast(err.message, "err");
      setBusy(button, false);
    }
  });

  // 仪表盘：有任务在跑时自动刷新列表
  function autoRefresh() {
    const hasActive = document.querySelector(".job-strip");
    if (!hasActive) return;
    let countdown = 12;
    const timer = setInterval(() => {
      countdown -= 1;
      if (countdown <= 0) {
        clearInterval(timer);
        window.location.reload();
      }
    }, 1000);
  }

  // -------------------------------------------------------------- 批量操作
  // 勾选行后显示工具条；动作与单台操作同一去重键，不会重复入队。
  function selectedIds() {
    return Array.from(document.querySelectorAll(".row-check:checked")).map((cb) => Number(cb.value));
  }

  function bindBulkBar() {
    const bar = document.getElementById("bulk-bar");
    if (!bar) return;
    const counter = document.getElementById("bulk-count");

    const update = () => {
      const ids = selectedIds();
      bar.hidden = ids.length === 0;
      if (counter) counter.textContent = ids.length;
    };
    document.querySelectorAll(".row-check").forEach((cb) => cb.addEventListener("change", update));
    const all = document.getElementById("check-all");
    if (all) {
      all.addEventListener("change", () => {
        document.querySelectorAll(".row-check").forEach((cb) => { cb.checked = all.checked; });
        update();
      });
    }
    update();
  }

  const BULK_LABELS = { start: "开机", stop: "关机", "hard-stop": "强制关机", reset: "重启", suspend: "挂起", delete: "删除" };

  async function submitBulk(button) {
    const ids = selectedIds();
    if (!ids.length) {
      toast("请先勾选要操作的虚拟机", "err");
      return;
    }
    const op = button.dataset.bulk;
    const label = BULK_LABELS[op] || op;
    if (op === "delete" && !confirm(`确定删除选中的 ${ids.length} 台虚拟机及其磁盘文件？此操作不可撤销。`)) return;
    if ((op === "stop" || op === "hard-stop" || op === "reset") && !confirm(`确定对选中的 ${ids.length} 台执行「${label}」？`)) return;

    setBusy(button, true, "提交中…");
    try {
      const data = await post(button.dataset.url || "/vms/bulk/", { action: op, ids });
      const queued = Number(data.queued) || 0;
      const failed = Number(data.failed) || 0;
      if (failed) {
        const first = (data.errors && data.errors[0] && data.errors[0].message) || "";
        toast(`已提交 ${queued} 个任务，${failed} 台失败：${first}`, "warn", 8000);
      } else {
        toast(`已提交 ${queued} 个「${label}」任务`, "ok");
      }
      setTimeout(() => window.location.reload(), 900);
    } catch (err) {
      toast(err.message, "err");
      setBusy(button, false);
    }
  }

  // Panel switching inside a page (VM detail). The chosen panel is kept in the
  // URL hash so a reload lands on the same tab; replaceState keeps tab clicks
  // out of the back-button history.
  function initTabs() {
    document.querySelectorAll("[data-tabs]").forEach((root) => {
      const tabs = Array.from(root.querySelectorAll("[data-tab]"));
      const panels = Array.from(root.querySelectorAll("[data-tab-panel]"));
      if (!tabs.length || !panels.length) return;

      const names = tabs.map((tab) => tab.dataset.tab);

      const activate = (name) => {
        tabs.forEach((tab) => {
          const on = tab.dataset.tab === name;
          tab.classList.toggle("is-active", on);
          tab.setAttribute("aria-selected", on ? "true" : "false");
        });
        panels.forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== name; });
      };

      tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
          activate(tab.dataset.tab);
          // Sandboxed iframes and file:// reject replaceState; a failed hash
          // update must not break the rest of the page init.
          try { history.replaceState(null, "", `#${tab.dataset.tab}`); } catch (_) { /* ignore */ }
        });
      });

      const fromHash = location.hash.slice(1);
      activate(names.includes(fromHash) ? fromHash : names[0]);
    });
  }

  window.vmm = { get, post, toast };

  document.addEventListener("DOMContentLoaded", () => {
    initTabs();
    autoRefresh();
    bindBulkBar();
  });
})();
