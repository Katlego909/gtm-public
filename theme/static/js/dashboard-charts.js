// theme/static/js/dashboard-charts.js
function initializeDashboardCharts(chartData) {
    // --- Chart Configuration ---
    Chart.defaults.font.family = "'Inter', sans-serif";
    Chart.defaults.color = '#64748b';
    Chart.defaults.scale.grid.borderColor = '#f1f5f9';

    function drawKpiProgress(id, value, color) {
        const ctx = document.getElementById(id);
        if(!ctx) return;
        new Chart(ctx.getContext('2d'), {
            type: 'doughnut',
            data: {
                datasets: [{
                    data: [value, 100-value],
                    backgroundColor: [color, '#f1f5f9'],
                    borderWidth: 0,
                    borderRadius: 20
                }]
            },
            options: {
                cutout: '75%',
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
                animation: { animateScale: true }
            }
        });
    }

    drawKpiProgress('kpiSessions', chartData.total_sessions || 0, '#4f46e5');
    drawKpiProgress('kpiCompleted', chartData.completed_items || 0, '#eab308');
    drawKpiProgress('kpiPending', chartData.pending_items || 0, '#ef4444');

    const ctxBar = document.getElementById('sessionsChart');
    if(ctxBar) {
        new Chart(ctxBar.getContext('2d'), {
            type: 'bar',
            data: {
                labels: chartData.days_labels || [],
                datasets: [{
                    label: 'Sessions',
                    data: chartData.sessions_per_day || [],
                    backgroundColor: '#6366f1',
                    borderRadius: 4,
                    barThickness: 24,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, border: { display: false }, grid: { borderDash: [4, 4], drawBorder: false } },
                    x: { grid: { display: false }, border: { display: false } }
                }
            }
        });
    }

    const ctxDonut = document.getElementById('statusDonut');
    if(ctxDonut) {
        new Chart(ctxDonut.getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: ['Completed', 'Pending'],
                datasets: [{
                    data: [chartData.completed_items || 0, chartData.pending_items || 0],
                    backgroundColor: ['#eab308', '#ef4444'],
                    borderWidth: 0
                }]
            },
            options: {
                cutout: '65%',
                responsive: true,
                maintainAspectRatio: false,
                layout: { padding: 20 },
                plugins: { 
                    legend: { position: 'bottom', labels: { usePointStyle: true, padding: 20, font: { size: 12 } } } 
                }
            }
        });
    }

    const channelData = {
        labels: (chartData.channel_data || []).map(c => c.name),
        datasets: [{
            data: (chartData.channel_data || []).map(c => c.percent),
            backgroundColor: (chartData.channel_data || []).map(c => c.color),
            borderWidth: 2,
            borderColor: '#fff',
        }]
    };
    const channelDonut = document.getElementById('channelDonut');
    if(channelDonut) {
        new Chart(channelDonut.getContext('2d'), {
            type: 'doughnut',
            data: channelData,
            options: {
                cutout: '70%',
                plugins: { legend: { display: false } },
                responsive: true,
                maintainAspectRatio: false,
            }
        });
    }
}
