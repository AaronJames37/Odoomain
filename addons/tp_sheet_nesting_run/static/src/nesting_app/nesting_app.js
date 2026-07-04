/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, markup, onMounted, onWillUnmount, useState } from "@odoo/owl";

export class TpNestingLiveApp extends Component {
    static template = "tp_sheet_nesting_run.LiveNestingApp";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.bus = useService("bus_service");
        this.state = useState({
            loading: true,
            runningRpc: false,
            session: null,
            selectedBatchId: false,
            pollTimer: null,
            csvFilename: "",
            csvTargetBatchId: false,
            csvOnlySourceId: false,
            manualDraftRows: this.makeManualDraftRows(),
            showSettings: false,
            showBatches: false,
        });
        this._busHandler = this.onBusUpdate.bind(this);
        this._busSubscribed = false;
        this._pollPromise = null;
        this._previewRunToken = 0;

        onMounted(async () => {
            await this.bootstrap();
            this.state.pollTimer = browser.setInterval(() => this.poll(), 1000);
        });
        onWillUnmount(() => {
            if (this.state.pollTimer) {
                browser.clearInterval(this.state.pollTimer);
            }
            if (this.state.session?.busChannel) {
                this.bus.deleteChannel(this.state.session.busChannel);
            }
            if (this._busSubscribed) {
                this.bus.unsubscribe("tp_nesting_preview_update", this._busHandler);
            }
        });
    }

    makeManualDraftRows(count = 5) {
        return Array.from({ length: count }, (_value, index) => ({
            key: `draft-${Date.now()}-${index}`,
            lengthMm: "",
            widthMm: "",
            quantity: "",
            label: "",
            isDraft: true,
        }));
    }

    async bootstrap() {
        this.state.loading = true;
        try {
            const session = await this.orm.call("tp.nesting.preview.session", "app_bootstrap", []);
            await this.applySession(session);
            this.bus.subscribe("tp_nesting_preview_update", this._busHandler);
            this._busSubscribed = true;
            await this.bus.addChannel(session.busChannel);
        } finally {
            this.state.loading = false;
        }
    }

    async applySession(session) {
        this.state.session = session;
        if (!this.state.selectedBatchId || !session.batches.some((b) => b.id === this.state.selectedBatchId)) {
            const first = session.batches.find((b) => b.selected) || session.batches[0];
            this.state.selectedBatchId = first ? first.id : false;
        }
        if (!this.state.csvTargetBatchId && session.batches.length) {
            this.state.csvTargetBatchId = session.batches[0].id;
        }
    }

    focusBatch(batchId) {
        if ((this.state.session?.batches || []).some((batch) => batch.id === batchId)) {
            this.state.selectedBatchId = batchId;
        }
    }

    focusManualCsvBatch(sourceProductId = false) {
        const sourceId = Number(sourceProductId || 0);
        const batches = [...(this.state.session?.batches || [])].reverse();
        const batch = batches.find((item) => {
            const key = String(item.groupKey || "");
            const isManualCsv = key.startsWith("manual_csv:") || key.startsWith("csv_only:");
            const sameSource = !sourceId || Number(item.sourceProductId || 0) === sourceId;
            return isManualCsv && sameSource;
        });
        if (batch) {
            this.state.selectedBatchId = batch.id;
            this.state.csvTargetBatchId = batch.id;
        }
    }

    onBusUpdate(payload) {
        if (!this.state.session || payload.session_id !== this.state.session.id) {
            return;
        }
        this.poll(true);
    }

    async poll(force = false) {
        const session = this.state.session;
        if (!session || (!force && this.state.loading)) {
            return;
        }
        if (this._pollPromise) {
            return this._pollPromise;
        }
        this._pollPromise = this.orm.silent.call("tp.nesting.preview.session", "poll", [
            [session.id],
            session.updateSequence || 0,
        ]).then(async (data) => {
            if (data.changed || force) {
                await this.applySession(data);
            }
        }).finally(() => {
            this._pollPromise = null;
        });
        return this._pollPromise;
    }

    get selectedBatch() {
        const session = this.state.session;
        if (!session) {
            return null;
        }
        return session.batches.find((batch) => batch.id === this.state.selectedBatchId) || session.batches[0] || null;
    }

    get previewHtml() {
        const result = this.selectedBatch?.bestResult;
        return result ? markup(result.svg || "") : "";
    }

    get selectedCount() {
        return (this.state.session?.batches || []).filter((batch) => batch.selected).length;
    }

    get manualBatches() {
        return (this.state.session?.batches || []).filter((batch) => {
            const key = String(batch.groupKey || "");
            return key.startsWith("manual_csv:") || key.startsWith("csv_only:");
        });
    }

    get activeOrderBatches() {
        return (this.state.session?.batches || []).filter((batch) => {
            const key = String(batch.groupKey || "");
            return !key.startsWith("manual_csv:") && !key.startsWith("csv_only:");
        });
    }

    get selectedOrderBatchCount() {
        return this.activeOrderBatches.filter((batch) => batch.selected).length;
    }

    get manualPanelQuantity() {
        return this.manualBatches.reduce((total, batch) => total + Number(batch.panelQuantity || 0), 0);
    }

    get manualRows() {
        return this.manualBatches.flatMap((batch) => batch.partRows || []);
    }

    get manualGridRows() {
        const existing = this.manualRows.map((row) => ({
            ...row,
            key: `part-${row.id}`,
            isDraft: false,
            active: row.active !== false,
        }));
        return [...existing, ...this.state.manualDraftRows];
    }

    get canPreview() {
        const session = this.state.session;
        return session && !["running", "committed"].includes(session.state) && this.selectedCount > 0;
    }

    get canCommit() {
        const session = this.state.session;
        return session && ["running", "done", "failed", "cancelled"].includes(session.state)
            && (session.batches || []).filter((batch) => batch.selected).every((batch) => !!batch.bestResult);
    }

    batchClass(batch) {
        const classes = ["tp_nesting_batch"];
        if (batch.id === this.state.selectedBatchId) {
            classes.push("is-active");
        }
        if (batch.selected) {
            classes.push("is-selected");
        }
        classes.push(`is-${batch.state}`);
        return classes.join(" ");
    }

    selectBatch(batch) {
        this.state.selectedBatchId = batch.id;
    }

    async toggleBatch(batch, ev) {
        const checked = ev.target.checked;
        const data = await this.orm.call("tp.nesting.preview.session", "update_batch", [
            [this.state.session.id],
            batch.id,
            { selected: checked },
        ]);
        await this.applySession(data);
        if (checked) {
            this.focusBatch(batch.id);
        }
    }

    async changeSource(batch, ev) {
        const productId = Number(ev.target.value || 0);
        const data = await this.orm.call("tp.nesting.preview.session", "update_batch", [
            [this.state.session.id],
            batch.id,
            { source_product_id: productId },
        ]);
        await this.applySession(data);
    }

    async changeInputMode(ev) {
        const mode = ev.target.value;
        const data = await this.orm.call("tp.nesting.preview.session", "set_input_mode", [
            [this.state.session.id],
            mode,
            this.state.csvOnlySourceId || false,
        ]);
        await this.applySession(data);
    }

    async refreshBatches() {
        const data = await this.orm.call("tp.nesting.preview.session", "refresh_batches", [[this.state.session.id]]);
        await this.applySession(data);
    }

    onKerfInput(ev) {
        this.state.session.kerfMm = Number(ev.target.value || 0);
    }

    async onCsvFile(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) {
            return;
        }
        this.state.csvFilename = file.name;
        try {
            await this.importCsv(file);
        } finally {
            ev.target.value = "";
        }
    }

    async importCsv(file) {
        if (!this.state.csvOnlySourceId) {
            this.notification.add(_t("Choose a Manual/CSV material first."), { type: "warning" });
            return;
        }
        const dataUrl = await this.readFileAsDataUrl(file);
        const encoded = dataUrl.split(",", 2)[1] || "";
        const data = await this.orm.call("tp.nesting.preview.session", "import_csv", [
            [this.state.session.id],
            encoded,
            this.state.csvFilename,
            this.state.csvTargetBatchId || false,
            false,
            this.state.csvOnlySourceId || false,
        ]);
        this.state.csvFilename = "";
        await this.applySession(data);
        this.focusManualCsvBatch(this.state.csvOnlySourceId);
    }

    updateManualDraft(row, field, value) {
        row[field] = value;
    }

    canSaveManualDraft(row) {
        return this.state.session
            && this.state.session.state !== "running"
            && Number(this.state.csvOnlySourceId || 0) > 0
            && Number(row.widthMm || 0) > 0
            && Number(row.lengthMm || 0) > 0
            && Number(row.quantity || 0) > 0;
    }

    clearManualDraftRow(row) {
        row.lengthMm = "";
        row.widthMm = "";
        row.quantity = "";
        row.label = "";
    }

    async addManualDraftRow(row) {
        if (!this.canSaveManualDraft(row)) {
            this.notification.add(_t("Enter a material, length, width and quantity first."), { type: "warning" });
            return;
        }
        const data = await this.orm.call("tp.nesting.preview.session", "add_manual_panel", [
            [this.state.session.id],
            row.widthMm,
            row.lengthMm,
            row.quantity || 1,
            row.label || "",
            this.state.csvTargetBatchId || false,
            this.state.csvOnlySourceId || false,
            false,
        ]);
        this.clearManualDraftRow(row);
        await this.applySession(data);
        this.focusManualCsvBatch(this.state.csvOnlySourceId);
    }

    async deleteManualPanel(row) {
        if (!row?.id || this.state.session.state === "running") {
            return;
        }
        const data = await this.orm.call("tp.nesting.preview.session", "delete_manual_panel", [
            [this.state.session.id],
            row.id,
        ]);
        await this.applySession(data);
        this.focusManualCsvBatch(this.state.csvOnlySourceId);
    }

    async toggleManualPanel(row) {
        if (!row?.id || this.state.session.state === "running") {
            return;
        }
        const data = await this.orm.call("tp.nesting.preview.session", "toggle_manual_panel", [
            [this.state.session.id],
            row.id,
            !row.active,
        ]);
        await this.applySession(data);
        this.focusManualCsvBatch(this.state.csvOnlySourceId);
    }

    readFileAsDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(file);
        });
    }

    async startPreview() {
        if (!this.canPreview) {
            return;
        }
        const selectedIds = this.state.session.batches.filter((batch) => batch.selected).map((batch) => batch.id);
        if (selectedIds.length && !selectedIds.includes(this.state.selectedBatchId)) {
            this.state.selectedBatchId = selectedIds[0];
        }
        const session = await this.orm.call("tp.nesting.preview.session", "start_preview", [
            [this.state.session.id],
            selectedIds,
            this.state.session.kerfMm,
        ]);
        await this.applySession(session);
        const runIds = session.batches.filter((batch) => batch.selected).map((batch) => batch.id);
        if (runIds.length) {
            this.state.selectedBatchId = runIds[0];
        }
        this.state.runningRpc = true;
        const runToken = ++this._previewRunToken;
        this.runPreviewBatches(runIds, runToken)
            .then((data) => this.applySession(data))
            .catch((error) => {
                this.notification.add(error.message || _t("Preview failed."), { type: "danger" });
            })
            .finally(() => {
                if (runToken === this._previewRunToken) {
                    this.state.runningRpc = false;
                    this.poll(true);
                }
            });
    }

    async runPreviewBatches(batchIds, runToken) {
        const sessionId = this.state.session.id;
        const queue = [...batchIds];
        const concurrency = Math.max(1, Math.min(
            Number(this.state.session.parallelBatchLimit || 1),
            queue.length
        ));
        const workers = Array.from({ length: concurrency }, async () => {
            while (queue.length && runToken === this._previewRunToken) {
                const batchId = queue.shift();
                const data = await this.orm.silent.call("tp.nesting.preview.session", "run_preview_batch", [
                    [sessionId],
                    batchId,
                ]);
                await this.applySession(data);
            }
        });
        await Promise.all(workers);
        return this.orm.silent.call("tp.nesting.preview.session", "finish_preview", [[sessionId]]);
    }

    async cancelPreview() {
        const data = await this.orm.call("tp.nesting.preview.session", "cancel", [[this.state.session.id]]);
        await this.applySession(data);
    }

    toggleSettings() {
        this.state.showSettings = !this.state.showSettings;
    }

    toggleBatches() {
        this.state.showBatches = !this.state.showBatches;
    }

    async commitBest() {
        if (!this.canCommit) {
            return;
        }
        const action = await this.orm.call("tp.nesting.preview.session", "commit_best", [[this.state.session.id]]);
        await this.action.doAction(action);
    }
}

registry.category("actions").add("tp_sheet_nesting_run.live_app", TpNestingLiveApp);
