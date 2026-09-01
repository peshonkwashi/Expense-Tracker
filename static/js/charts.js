/* Dashboard charts (section 5.8).
 *
 * The report commits to the system working with no internet connection
 * (section 5.11), but Chart.js is loaded from a CDN. Rather than let the
 * dashboard render an empty box when offline, every chart degrades to an
 * accessible CSS bar chart built from the same data.
 *
 * To remove the dependency entirely, download chart.umd.js into static/js/
 * and point the script tag in base.html at it.
 */
(function () {
    'use strict';

    var PALETTE = {
        budget: '#14532d',
        actual: '#b45309',
        grid: '#e3e9f0',
        ink: '#5b6b7f'
    };

    function readData(id) {
        var holder = document.getElementById(id);
        if (!holder) { return null; }
        try {
            return JSON.parse(holder.textContent);
        } catch (err) {
            console.warn('Chart data for ' + id + ' could not be parsed', err);
            return null;
        }
    }

    function money(value) {
        return Number(value).toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2
        });
    }

    /* Accessible fallback: labelled proportional bars, no library needed. */
    function renderFallback(canvas, labels, series) {
        var max = 0;
        series.forEach(function (set) {
            set.values.forEach(function (value) { max = Math.max(max, Number(value) || 0); });
        });
        if (max <= 0) { max = 1; }

        var wrap = document.createElement('div');
        wrap.className = 'fallback-chart';

        labels.forEach(function (label, index) {
            series.forEach(function (set, setIndex) {
                var row = document.createElement('div');
                row.className = 'fallback-row';

                var name = document.createElement('span');
                name.textContent = setIndex === 0 ? label : '';

                var track = document.createElement('span');
                track.className = 'fallback-track';
                var fill = document.createElement('i');
                if (set.role === 'actual') { fill.className = 'actual'; }
                fill.style.width = Math.max(1, (Number(set.values[index]) || 0) / max * 100) + '%';
                track.appendChild(fill);

                var amount = document.createElement('span');
                amount.className = 'muted';
                amount.textContent = set.label + ': ' + money(set.values[index] || 0);

                row.appendChild(name);
                row.appendChild(track);
                row.appendChild(amount);
                wrap.appendChild(row);
            });
        });

        var box = canvas.parentNode;
        box.style.height = 'auto';
        box.replaceChild(wrap, canvas);
    }

    function buildBar(canvasId, dataId, series, options) {
        var canvas = document.getElementById(canvasId);
        var data = readData(dataId);
        if (!canvas || !data || !data.labels || !data.labels.length) {
            if (canvas) {
                canvas.parentNode.innerHTML =
                    '<p class="empty">Not enough data to chart yet.</p>';
            }
            return;
        }

        var sets = series.map(function (spec) {
            return {
                label: spec.label,
                role: spec.role,
                values: data[spec.key] || []
            };
        });

        if (typeof window.Chart === 'undefined') {
            renderFallback(canvas, data.labels, sets);
            return;
        }

        new window.Chart(canvas.getContext('2d'), {
            type: options.type || 'bar',
            data: {
                labels: data.labels,
                datasets: sets.map(function (set) {
                    return {
                        label: set.label,
                        data: set.values,
                        backgroundColor: set.role === 'actual'
                            ? PALETTE.actual : PALETTE.budget,
                        borderRadius: 4,
                        maxBarThickness: 42
                    };
                })
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: sets.length > 1, labels: { boxWidth: 12 } },
                    tooltip: {
                        callbacks: {
                            label: function (context) {
                                return context.dataset.label + ': ' +
                                    money(context.parsed.y) + ' ' + (options.currency || '');
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: PALETTE.grid },
                        ticks: { color: PALETTE.ink, callback: money }
                    },
                    x: { grid: { display: false }, ticks: { color: PALETTE.ink } }
                }
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var currency = document.body.getAttribute('data-currency') || '';

        buildBar('budgetChart', 'budget-chart-data', [
            { key: 'recommended', label: 'Recommended budget', role: 'budget' },
            { key: 'spent', label: 'Spent so far', role: 'actual' }
        ], { currency: currency });

        buildBar('cycleChart', 'cycle-chart-data', [
            { key: 'values', label: 'Spending', role: 'budget' }
        ], { currency: currency });
    });
}());
