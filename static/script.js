(function () {
    const state = {
        data: null,
        tableRows: [],
        filteredRows: [],
        currentPage: 1,
        pageSize: 20,
    };

    const refs = {
        root: document.documentElement,
        resultsShell: document.getElementById("results-shell"),
        themeToggle: document.getElementById("theme-toggle"),
        uploadForm: document.getElementById("upload-form"),
        dropZone: document.getElementById("drop-zone"),
        fileInput: document.getElementById("dataset"),
        fileName: document.getElementById("file-name"),
        loader: document.getElementById("loader-overlay"),
        loaderText: document.getElementById("loader-text"),
        toast: document.getElementById("toast"),
        serverError: document.getElementById("server-error"),
        kSlider: document.getElementById("k-slider"),
        kValue: document.getElementById("k-value"),
        optimalK: document.getElementById("optimal-k"),
        downloadCsvBtn: document.getElementById("download-csv-btn"),
        downloadPdfBtn: document.getElementById("download-pdf-btn"),
        downloadElbowBtn: document.getElementById("download-elbow-btn"),
        downloadClusterBtn: document.getElementById("download-cluster-btn"),
        saveResultBtn: document.getElementById("save-result-btn"),
        kpiGrid: document.getElementById("kpi-grid"),
        insightCards: document.getElementById("insight-cards"),
        savedResultsList: document.getElementById("saved-results-list"),
        tableSearch: document.getElementById("table-search"),
        clusterFilter: document.getElementById("cluster-filter"),
        sortBy: document.getElementById("sort-by"),
        tableBody: document.getElementById("table-body"),
        pagination: document.getElementById("pagination"),
    };

    function showLoader(text) {
        refs.loaderText.textContent = text || "Processing...";
        refs.loader.classList.add("visible");
    }

    function hideLoader() {
        refs.loader.classList.remove("visible");
    }

    function showToast(message, type) {
        refs.toast.textContent = message;
        refs.toast.className = "toast show " + (type || "success");
        setTimeout(() => {
            refs.toast.classList.remove("show");
        }, 2600);
    }

    async function fetchJson(url, options) {
        const response = await fetch(url, options);

        if (response.status === 401) {
            window.location.href = "/login";
            throw new Error("Session expired. Redirecting to login.");
        }

        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || "Request failed.");
        }
        return payload;
    }

    function setTheme(theme) {
        refs.root.setAttribute("data-theme", theme);
        localStorage.setItem("theme", theme);
        const label = refs.themeToggle.querySelector("span");
        label.textContent = theme === "dark" ? "Dark" : "Light";
    }

    function initTheme() {
        const saved = localStorage.getItem("theme") || "dark";
        setTheme(saved);
        refs.themeToggle.addEventListener("click", function () {
            const next = refs.root.getAttribute("data-theme") === "dark" ? "light" : "dark";
            setTheme(next);
            renderCharts();
        });
    }

    async function fetchDashboardData(k) {
        const url = k ? `/api/process?k=${k}` : "/api/process";
        return fetchJson(url);
    }

    function updateKpiCards() {
        const analytics = state.data.analytics;
        refs.kpiGrid.innerHTML = `
            <div class="kpi-card fade-up"><div class="kpi-title">Total Customers</div><div class="kpi-value">${analytics.total_customers}</div></div>
            <div class="kpi-card fade-up"><div class="kpi-title">Optimal K</div><div class="kpi-value">${analytics.optimal_k}</div></div>
            <div class="kpi-card fade-up"><div class="kpi-title">Avg Income</div><div class="kpi-value">${analytics.average_income}</div></div>
            <div class="kpi-card fade-up"><div class="kpi-title">Avg Spending Score</div><div class="kpi-value">${analytics.average_spending_score}</div></div>
        `;
    }

    function segmentClass(segment) {
        if (segment.includes("High Income - High Spending")) {
            return "segment-high";
        }
        if (segment.includes("High Income - Low Spending")) {
            return "segment-warning";
        }
        if (segment.includes("Low Income - High Spending")) {
            return "segment-growth";
        }
        return "segment-low";
    }

    function updateInsights() {
        refs.insightCards.innerHTML = state.data.cluster_details
            .map((cluster) => {
                return `
                    <div class="cluster-card ${segmentClass(cluster.segment)} fade-up">
                        <div class="title">
                            <span>Cluster ${cluster.cluster}</span>
                            <span>${cluster.percentage}%</span>
                        </div>
                        <div class="meta">${cluster.segment}</div>
                        <div class="meta">Avg Income: ${cluster.avg_income} | Avg Score: ${cluster.avg_score}</div>
                        <div class="recommendation">${cluster.recommendation}</div>
                    </div>
                `;
            })
            .join("");
    }

    function plotBackgroundColor() {
        return refs.root.getAttribute("data-theme") === "light" ? "rgba(255,255,255,0.65)" : "rgba(255,255,255,0.06)";
    }

    function plotTextColor() {
        return refs.root.getAttribute("data-theme") === "light" ? "#1a2342" : "#e8efff";
    }

    function renderElbowChart() {
        const kValues = state.data.k_values;
        const inertiaValues = state.data.inertia_values;

        const elbowTrace = {
            x: kValues,
            y: inertiaValues,
            mode: "lines+markers",
            name: "WCSS",
            marker: { color: "#4f7cff", size: 8 },
            line: { width: 2.5, color: "#4f7cff" },
            hovertemplate: "K=%{x}<br>WCSS=%{y}<extra></extra>",
        };

        const optimalTrace = {
            x: [state.data.optimal_k],
            y: [inertiaValues[kValues.indexOf(state.data.optimal_k)]],
            mode: "markers",
            name: "Optimal K",
            marker: { color: "#ff5b76", size: 12, symbol: "diamond" },
            hovertemplate: "Optimal K=%{x}<extra></extra>",
        };

        Plotly.react(
            "elbow-chart",
            [elbowTrace, optimalTrace],
            {
                margin: { l: 55, r: 20, t: 30, b: 45 },
                paper_bgcolor: "transparent",
                plot_bgcolor: plotBackgroundColor(),
                font: { color: plotTextColor() },
                xaxis: { title: "K" },
                yaxis: { title: "WCSS" },
                legend: { orientation: "h", y: 1.12 },
            },
            { responsive: true, displaylogo: false }
        );
    }

    function renderClusterChart() {
        const clusters = [...new Set(state.data.points.map((p) => p.cluster))].sort((a, b) => a - b);
        const traces = clusters.map((clusterId) => {
            const clusterPoints = state.data.points.filter((p) => p.cluster === clusterId);
            return {
                x: clusterPoints.map((p) => p.income),
                y: clusterPoints.map((p) => p.score),
                mode: "markers",
                type: "scattergl",
                name: `Cluster ${clusterId}`,
                marker: { size: 8, opacity: 0.8 },
                customdata: clusterPoints.map((p) => [p.customer_id, p.cluster]),
                hovertemplate:
                    "Customer %{customdata[0]}<br>Income: %{x}<br>Spending Score: %{y}<br>Cluster: %{customdata[1]}<extra></extra>",
            };
        });

        traces.push({
            x: state.data.centroids.map((c) => c.income),
            y: state.data.centroids.map((c) => c.score),
            mode: "markers+text",
            name: "Centroids",
            marker: { color: "#ffb020", size: 16, symbol: "x" },
            text: state.data.centroids.map((c) => `C${c.cluster}`),
            textposition: "top center",
            hovertemplate: "Centroid %{text}<br>Income: %{x}<br>Score: %{y}<extra></extra>",
        });

        Plotly.react(
            "cluster-chart",
            traces,
            {
                margin: { l: 55, r: 20, t: 30, b: 45 },
                paper_bgcolor: "transparent",
                plot_bgcolor: plotBackgroundColor(),
                font: { color: plotTextColor() },
                xaxis: { title: "Annual Income (k$)" },
                yaxis: { title: "Spending Score (1-100)" },
            },
            {
                responsive: true,
                displaylogo: false,
                scrollZoom: true,
                modeBarButtonsToAdd: ["zoom2d", "pan2d", "resetScale2d"],
            }
        );
    }

    function renderCharts() {
        if (!state.data) {
            return;
        }
        renderElbowChart();
        renderClusterChart();
    }

    function updateClusterFilterOptions() {
        refs.clusterFilter.innerHTML = '<option value="all">All Clusters</option>';
        state.data.cluster_ids.forEach((clusterId) => {
            const option = document.createElement("option");
            option.value = String(clusterId);
            option.textContent = `Cluster ${clusterId}`;
            refs.clusterFilter.appendChild(option);
        });
    }

    function sortRows(rows) {
        const sortMode = refs.sortBy.value;
        const sorted = [...rows];

        sorted.sort((a, b) => {
            if (sortMode === "income-desc") {
                return b["Annual Income (k$)"] - a["Annual Income (k$)"];
            }
            if (sortMode === "income-asc") {
                return a["Annual Income (k$)"] - b["Annual Income (k$)"];
            }
            if (sortMode === "score-desc") {
                return b["Spending Score (1-100)"] - a["Spending Score (1-100)"];
            }
            if (sortMode === "score-asc") {
                return a["Spending Score (1-100)"] - b["Spending Score (1-100)"];
            }
            if (sortMode === "cluster-desc") {
                return b.Cluster - a.Cluster;
            }
            return a.Cluster - b.Cluster;
        });

        return sorted;
    }

    function applyTableFilters() {
        let rows = [...state.tableRows];
        const query = refs.tableSearch.value.trim().toLowerCase();
        const selectedCluster = refs.clusterFilter.value;

        if (selectedCluster !== "all") {
            rows = rows.filter((row) => String(row.Cluster) === selectedCluster);
        }

        if (query) {
            rows = rows.filter((row) => {
                const rowText = `${row["Annual Income (k$)"]} ${row["Spending Score (1-100)"]} ${row.Cluster}`.toLowerCase();
                return rowText.includes(query);
            });
        }

        state.filteredRows = sortRows(rows);
        state.currentPage = 1;
        renderTable();
    }

    function renderPagination(totalPages) {
        refs.pagination.innerHTML = "";
        if (totalPages <= 1) {
            return;
        }

        for (let page = 1; page <= totalPages; page += 1) {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = String(page);
            if (page === state.currentPage) {
                button.classList.add("active");
            }
            button.addEventListener("click", function () {
                state.currentPage = page;
                renderTable();
            });
            refs.pagination.appendChild(button);
        }
    }

    function renderTable() {
        const rows = state.filteredRows.length ? state.filteredRows : state.tableRows;
        const totalPages = Math.max(1, Math.ceil(rows.length / state.pageSize));
        state.currentPage = Math.min(state.currentPage, totalPages);
        const start = (state.currentPage - 1) * state.pageSize;
        const end = start + state.pageSize;
        const visibleRows = rows.slice(start, end);

        refs.tableBody.innerHTML = visibleRows
            .map(
                (row) => `
                <tr>
                    <td>${row["Annual Income (k$)"]}</td>
                    <td>${row["Spending Score (1-100)"]}</td>
                    <td>${row.Cluster}</td>
                </tr>
            `
            )
            .join("");

        renderPagination(totalPages);
    }

    function updateDownloadLink() {
        refs.downloadCsvBtn.href = `/download/csv?k=${state.data.selected_k}`;
        refs.downloadPdfBtn.href = `/download/pdf?k=${state.data.selected_k}`;
    }

    function updateHeaderStats() {
        refs.kValue.textContent = String(state.data.selected_k);
        refs.optimalK.textContent = String(state.data.optimal_k);
        refs.kSlider.min = String(state.data.k_range.min);
        refs.kSlider.max = String(state.data.k_range.max);
        refs.kSlider.value = String(state.data.selected_k);
    }

    function updateUIFromData() {
        refs.resultsShell.classList.remove("hidden");
        updateHeaderStats();
        updateKpiCards();
        updateInsights();

        state.tableRows = state.data.table_rows;
        state.filteredRows = [...state.tableRows];

        updateClusterFilterOptions();
        renderTable();
        renderCharts();
        updateDownloadLink();
        loadSavedResults();
    }

    function renderSavedResults(items) {
        if (!refs.savedResultsList) {
            return;
        }

        if (!items.length) {
            refs.savedResultsList.innerHTML = '<p class="empty-text">No saved results yet.</p>';
            return;
        }

        refs.savedResultsList.innerHTML = items
            .map(
                (item) => `
                <div class="saved-result-item">
                    <strong>K=${item.selected_k} | Optimal=${item.optimal_k}</strong>
                    <span class="meta">Customers: ${item.total_customers} | Avg Income: ${item.average_income} | Avg Score: ${item.average_spending_score}</span>
                    <span class="meta">Saved: ${item.created_at.replace("T", " ")}</span>
                </div>
            `
            )
            .join("");
    }

    async function loadSavedResults() {
        try {
            const data = await fetchJson("/api/my-results");
            renderSavedResults(data);
        } catch (error) {
            if (!String(error.message).includes("Redirecting")) {
                showToast(error.message, "error");
            }
        }
    }

    async function saveCurrentResult() {
        if (!state.data) {
            showToast("Process data before saving.", "error");
            return;
        }

        try {
            showLoader("Saving result...");
            const result = await fetchJson("/api/save-result", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ selected_k: state.data.selected_k }),
            });
            showToast(result.message || "Result saved.", "success");
            loadSavedResults();
        } catch (error) {
            if (!String(error.message).includes("Redirecting")) {
                showToast(error.message, "error");
            }
        } finally {
            hideLoader();
        }
    }

    async function handleKChange(newK) {
        try {
            showLoader("Recomputing clusters...");
            const data = await fetchDashboardData(newK);
            state.data = data;
            updateUIFromData();
            showToast(`Updated clusters with K=${newK}`, "success");
        } catch (error) {
            showToast(error.message, "error");
        } finally {
            hideLoader();
        }
    }

    function initKControls() {
        refs.kSlider.addEventListener("input", function () {
            refs.kValue.textContent = refs.kSlider.value;
        });

        refs.kSlider.addEventListener("change", function () {
            handleKChange(Number(refs.kSlider.value));
        });
    }

    function initUploadForm() {
        refs.uploadForm.addEventListener("submit", function () {
            showLoader("Uploading and processing...");
            document.getElementById("step-process").classList.add("active");
        });

        refs.fileInput.addEventListener("change", function () {
            const file = refs.fileInput.files[0];
            refs.fileName.textContent = file ? file.name : "No file selected";
        });

        ["dragenter", "dragover"].forEach((eventName) => {
            refs.dropZone.addEventListener(eventName, function (event) {
                event.preventDefault();
                refs.dropZone.classList.add("dragover");
            });
        });

        ["dragleave", "drop"].forEach((eventName) => {
            refs.dropZone.addEventListener(eventName, function (event) {
                event.preventDefault();
                refs.dropZone.classList.remove("dragover");
            });
        });

        refs.dropZone.addEventListener("drop", function (event) {
            const droppedFile = event.dataTransfer.files[0];
            if (!droppedFile) {
                return;
            }
            const transfer = new DataTransfer();
            transfer.items.add(droppedFile);
            refs.fileInput.files = transfer.files;
            refs.fileName.textContent = droppedFile.name;
        });
    }

    function initTableControls() {
        refs.tableSearch.addEventListener("input", applyTableFilters);
        refs.clusterFilter.addEventListener("change", applyTableFilters);
        refs.sortBy.addEventListener("change", applyTableFilters);
    }

    function initExportButtons() {
        refs.downloadElbowBtn.addEventListener("click", async function () {
            const url = await Plotly.toImage("elbow-chart", { format: "png", width: 1100, height: 700 });
            const a = document.createElement("a");
            a.href = url;
            a.download = `elbow_k${state.data.selected_k}.png`;
            a.click();
        });

        refs.downloadClusterBtn.addEventListener("click", async function () {
            const url = await Plotly.toImage("cluster-chart", { format: "png", width: 1100, height: 700 });
            const a = document.createElement("a");
            a.href = url;
            a.download = `clusters_k${state.data.selected_k}.png`;
            a.click();
        });

        refs.saveResultBtn.addEventListener("click", saveCurrentResult);
    }

    function initCollapsibleSections() {
        const buttons = document.querySelectorAll("[data-collapse-target]");
        buttons.forEach((btn) => {
            btn.addEventListener("click", function () {
                const targetId = btn.getAttribute("data-collapse-target");
                const body = document.getElementById(targetId);
                if (body) {
                    body.classList.toggle("collapsed");
                }
            });
        });
    }

    function initInitialData() {
        const bootElement = document.getElementById("boot-data");
        let bootPayload = null;

        if (bootElement && bootElement.textContent) {
            try {
                bootPayload = JSON.parse(bootElement.textContent);
            } catch (error) {
                showToast("Failed to read initial dashboard payload.", "error");
            }
        }

        if (bootPayload && bootPayload.success && bootPayload.data) {
            state.data = bootPayload.data;
            updateUIFromData();
            showToast("Dataset processed successfully", "success");
        } else {
            loadSavedResults();
        }

        if (refs.serverError) {
            showToast(refs.serverError.getAttribute("data-message"), "error");
        }
    }

    function initIcons() {
        if (window.lucide) {
            window.lucide.createIcons();
        }
    }

    function init() {
        initTheme();
        initUploadForm();
        initKControls();
        initTableControls();
        initExportButtons();
        initCollapsibleSections();
        initInitialData();
        initIcons();
    }

    init();
})();
