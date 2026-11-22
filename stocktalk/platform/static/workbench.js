"use strict";
(() => {
    function query(selector, root = document) {
        const result = root.querySelector(selector);
        if (!result)
            throw new Error(`Missing interface element: ${selector}`);
        return result;
    }
    const $ = query;
    const formField = (form, name) => {
        const field = form.elements.namedItem(name);
        if (!(field instanceof HTMLInputElement) && !(field instanceof HTMLSelectElement))
            throw new Error('Missing form field');
        return field;
    };
    const checked = (form, name) => formField(form, name).checked;
    const message = (error) => error instanceof Error ? error.message : '请求失败';
    const desktop = document.body.dataset.desktop === "true";
    if (desktop)
        document.addEventListener("click", event => {
            const anchor = event.target instanceof Element ? event.target.closest("a") : null;
            if (anchor && new URL(anchor.href, location.href).origin !== location.origin) {
                event.preventDefault();
                window.open(anchor.href, "_blank", "noopener,noreferrer");
            }
        });
    const csrf = $('meta[name="csrf-token"]').content;
    let selected = null, offset = 0, busy = false, pendingKey = null;
    const statusNames = { queued: "排队中", running: "生成中", succeeded: "待审阅", failed: "失败", interrupted: "已中断", pending: "等待发布", submitting: "发布中", action_required: "需要本机操作", outcome_unknown: "结果待核对" };
    function notice(message) { $("#notice").textContent = message; $("#notice").hidden = !message; }
    async function api(path, data, key) {
        const response = await fetch(path, { method: data === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, ...(key ? { "Idempotency-Key": key } : {}) }, ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
        const result = await response.json();
        if (!response.ok)
            throw new Error(`${result.error?.code || response.status} · ${result.error?.message || "请求失败"}`);
        return result;
    }
    const textNode = (tag, text, cls) => { const e = document.createElement(tag); e.textContent = text; if (cls)
        e.className = cls; return e; };
    async function loadTypes() {
        const result = await api("/api/types");
        for (const t of result.types) {
            if (!desktop && t.status === "enabled")
                $("#type").append(new Option(t.display_name, t.id));
            $("#filter-type").append(new Option(t.display_name + (t.status === "disabled" ? "（已停用）" : ""), t.id));
        }
        if (!desktop) {
            $("#type").options[0].text = "选择类型";
            $("#type").value = result.types.find(t => t.status === "enabled")?.id || "";
        }
        if (Object.keys(result.diagnostics).length)
            notice("部分类型声明无效，请检查服务配置：" + Object.keys(result.diagnostics).join("、"));
    }
    async function loadModels() {
        if (desktop)
            return;
        $("#generate").disabled = true;
        $("#model").replaceChildren(new Option("正在获取 oneapi 清单…", ""));
        try {
            const result = await api("/api/models");
            $("#model").replaceChildren(new Option("选择对话模型", ""));
            for (const m of result.models)
                $("#model").append(new Option(m.id, m.id));
            $("#model").value = result.models[0]?.id || "";
            $("#generate").disabled = !result.models.length;
            notice(result.models.length ? "" : "oneapi 暂无可用模型，请联系维护方并刷新清单。");
        }
        catch (e) {
            $("#model").replaceChildren(new Option("模型清单不可用", ""));
            notice(message(e));
        }
    }
    async function list() {
        const filters = new URLSearchParams([...new FormData($("#filter-form"))].map(([k, v]) => [k, String(v)]));
        for (const [key, value] of [...filters])
            if (!value)
                filters.delete(key);
        if (filters.has("until"))
            filters.set("until", filters.get("until") + "T23:59:59.999Z");
        filters.set("limit", "20");
        filters.set("offset", String(offset));
        const result = await api("/api/tasks?" + filters);
        const area = $("#task-list");
        area.replaceChildren();
        if (!result.tasks.length)
            area.append(textNode("p", "还没有任务。选择类型与模型，生成第一条视频。", "placeholder"));
        for (const task of result.tasks) {
            const button = document.createElement("button");
            button.className = "task-row" + (selected === task.id ? " selected" : "");
            const top = textNode("div", "", "row-top");
            top.append(textNode("strong", task.stock), textNode("span", statusNames[task.status] || task.status, "badge"));
            button.append(top, textNode("div", `${task.type_id} · ${task.model}`), textNode("small", `${task.created_at.replace("T", " ").slice(0, 19)} UTC · ${task.stage}`));
            button.addEventListener("click", () => open(task.id));
            area.append(button);
        }
        $("#page-number").textContent = String(offset / 20 + 1);
        $("#previous").disabled = offset === 0;
        $("#next").disabled = result.tasks.length < 20;
    }
    async function open(id) {
        selected = id;
        location.hash = id;
        const [task, pubs] = await Promise.all([api("/api/tasks/" + id), api("/api/tasks/" + id + "/publications")]);
        const fragment = $("#detail-template").content.cloneNode(true);
        function d(s) { return query(s, fragment); }
        d(".detail-title").textContent = task.result?.stock_name || task.stock;
        d(".detail-status").textContent = statusNames[task.status] || task.status;
        d(".detail-id").textContent = `任务 ${task.id}`;
        if (task.error) {
            d(".detail-error").hidden = false;
            d(".detail-error").textContent = task.error.code + " · " + task.error.message;
        }
        const script = task.result?.approved_script;
        for (const turn of script?.turns || []) {
            const p = textNode("p", "");
            p.append(textNode("b", (turn.character_name || turn.speaker) + "："), document.createTextNode(turn.line));
            d(".script").append(p);
        }
        if (!script)
            d(".script").textContent = task.status === "running" ? "正在生成脚本与媒体，请稍候…" : "暂无可审阅脚本。";
        for (const [key, value] of [["视频类型", task.type_id], ["对话模型", task.model], ["画幅", task.result?.canvas || "等待生成"], ["当前阶段", task.stage]])
            d(".metadata").append(textNode("dt", key), textNode("dd", value || ""));
        for (const artifact of task.artifacts) {
            const url = `/api/tasks/${id}/artifacts/${artifact.id}`;
            const a = textNode("a", artifact.name);
            a.href = url;
            a.download = artifact.name;
            d(".artifacts").append(a);
            if (artifact.kind === "mp4" && d("video").hidden) {
                d("video").src = url;
                d("video").hidden = false;
            }
            if (artifact.kind === "png" && d("img").hidden) {
                d("img").src = url;
                d("img").hidden = false;
            }
        }
        if (!pubs.publications.length)
            d(".reports").append(textNode("p", "尚未发布。先审阅成片，再确认目标平台。", "hint"));
        for (const p of pubs.publications) {
            const report = textNode("div", `${p.platform} · ${p.status === "succeeded" ? "已发布" : statusNames[p.status] || p.status} · ${p.report?.message || "等待执行"}`, "report");
            if (!desktop && ["failed", "action_required"].includes(p.status)) {
                const b = textNode("button", "补发此平台", "text-button");
                b.onclick = () => submitPublication(id, p.platform, p.id);
                report.append(b);
            }
            d(".reports").append(report);
        }
        if (!desktop) {
            const form = d(".publish-form");
            form.hidden = task.status !== "succeeded";
            form.onsubmit = e => { e.preventDefault(); submitPublication(id, formField(form, "platform").value); };
            d(".preflight").onclick = async () => { try {
                const r = await api("/api/preflight", { platforms: [formField(form, "platform").value] });
                notice(Object.values(r.platforms).map(p => p.message).join("；"));
            }
            catch (e) {
                notice(message(e));
            } };
        }
        // Do not replace an active form or a playing video during background polling.
        const old = $("#detail");
        const prior = old.querySelector(".publish-form");
        if (prior && old.dataset.task === id && !desktop) {
            for (const name of ["platform", "schedule"])
                formField(d(".publish-form"), name).value = formField(prior, name).value;
            for (const name of ["ai", "reviewed"])
                formField(d(".publish-form"), name).checked = checked(prior, name);
        }
        old.dataset.task = id;
        old.dataset.status = task.status;
        old.dataset.publications = JSON.stringify(pubs.publications.map(p => [p.id, p.status]));
        old.replaceChildren(fragment);
    }
    async function submitPublication(id, platform, retryOf) {
        const form = $(".publish-form");
        if (!form.reportValidity())
            return;
        try {
            await api(`/api/tasks/${id}/publications`, { platform, schedule: formField(form, "schedule").value, ai_declaration: checked(form, "ai"), reviewed: checked(form, "reviewed"), ...(retryOf ? { retry_of: retryOf } : {}) });
            notice("发布已入队。请查看报告；结果未知时请先核对远端。");
            await open(id);
        }
        catch (e) {
            notice(message(e));
        }
    }
    if (!desktop) {
        $("#create-form").onsubmit = async (e) => { e.preventDefault(); if (busy)
            return; busy = true; $("#generate").disabled = true; try {
            const data = Object.fromEntries(new FormData($("#create-form")));
            const fingerprint = JSON.stringify(data);
            if (!pendingKey || pendingKey.fingerprint !== fingerprint)
                pendingKey = { fingerprint, key: crypto.randomUUID() };
            const task = await api("/api/tasks", data, pendingKey.key);
            notice("任务已入队，生成完成后可审阅成片。");
            await list();
            await open(task.id);
            pendingKey = null;
        }
        catch (e) {
            notice(message(e));
        }
        finally {
            busy = false;
            $("#generate").disabled = false;
        } };
        $("#refresh-models").onclick = loadModels;
    }
    $("#filter-form").onsubmit = e => { e.preventDefault(); offset = 0; list().catch(e => notice(message(e))); };
    $("#refresh").onclick = () => Promise.all([list(), selected ? open(selected) : Promise.resolve()]).catch(e => notice(message(e)));
    $("#previous").onclick = () => { offset = Math.max(0, offset - 20); list().catch(e => notice(message(e))); };
    $("#next").onclick = () => { offset += 20; list().catch(e => notice(message(e))); };
    Promise.all([loadTypes(), loadModels(), list()]).then(() => { if (location.hash.length > 1)
        return open(location.hash.slice(1)); }).catch(e => notice(message(e)));
    window.addEventListener("hashchange", () => { const id = location.hash.slice(1); if (id && id !== selected)
        open(id).catch(e => notice(message(e))); });
    setInterval(async () => {
        if (document.hidden)
            return;
        try {
            await list();
            if (selected) {
                const t = await api("/api/tasks/" + selected);
                const pubs = await api("/api/tasks/" + selected + "/publications");
                const changed = $("#detail").dataset.status !== t.status || $("#detail").dataset.publications !== JSON.stringify(pubs.publications.map(p => [p.id, p.status]));
                if (changed || ["queued", "running"].includes($("#detail").dataset.status || "") || !$("#detail").querySelector("video"))
                    await open(selected);
                $("#detail").dataset.status = t.status;
            }
        }
        catch (e) {
            notice(message(e));
        }
    }, 2500);
})();
