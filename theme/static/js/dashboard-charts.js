// theme/static/js/dashboard-charts.js
function initializeDashboardCharts(chartData) {
    // --- Chart Configuration ---
    Chart.defaults.font.family = "'Inter', sans-serif";
    Chart.defaults.color = '#64748b';
    Chart.defaults.scale.grid.borderColor = '#f1f5f9';

    function drawKpiProgress(id, value, color) {
        const ctx = document.getElementById(id);
        if(!ctx) return;
        
        // Ensure minimum visible progress for better UX
        const displayValue = Math.max(value || 0, 5);
        const actualValue = value || 0;
        
        new Chart(ctx.getContext('2d'), {
            type: 'doughnut',
            data: {
                datasets: [{
                    data: [displayValue, 100-displayValue],
                    backgroundColor: [actualValue > 0 ? color : '#f1f5f9', '#f1f5f9'],
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
        const hasData = chartData.sessions_per_day && chartData.sessions_per_day.some(v => v > 0);
        const barData = hasData ? chartData.sessions_per_day : [0, 0, 1, 0, 0, 0, 0]; // Show hint of activity
        
        new Chart(ctxBar.getContext('2d'), {
            type: 'bar',
            data: {
                labels: chartData.days_labels || ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
                datasets: [{
                    label: 'Sessions',
                    data: barData,
                    backgroundColor: hasData ? '#6366f1' : '#e2e8f0',
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
        const completedCount = chartData.completed_items || 0;
        const pendingCount = chartData.pending_items || 0;
        const hasStatusData = completedCount > 0 || pendingCount > 0;
        
        const donutData = hasStatusData ? [completedCount, pendingCount] : [1, 1]; // Equal placeholder
        const donutColors = hasStatusData ? ['#eab308', '#ef4444'] : ['#e2e8f0', '#f1f5f9'];
        
        new Chart(ctxDonut.getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: ['Completed', 'Pending'],
                datasets: [{
                    data: donutData,
                    backgroundColor: donutColors,
                    borderWidth: 0
                }]
            },
            options: {
                cutout: '65%',
                responsive: true,
                maintainAspectRatio: false,
                layout: { padding: 20 },
                plugins: { 
                    legend: { 
                        position: 'bottom', 
                        labels: { 
                            usePointStyle: true, 
                            padding: 20, 
                            font: { size: 12 },
                            color: hasStatusData ? '#64748b' : '#cbd5e1'
                        } 
                    },
                    tooltip: {
                        enabled: hasStatusData
                    }
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
    
    // Handle empty channel data
    if (!chartData.channel_data || chartData.channel_data.length === 0) {
        channelData.labels = ['No data available'];
        channelData.datasets[0].data = [100];
        channelData.datasets[0].backgroundColor = ['#f1f5f9'];
    }
    
    const channelDonut = document.getElementById('channelDonut');
    if(channelDonut) {
        new Chart(channelDonut.getContext('2d'), {
            type: 'doughnut',
            data: channelData,
            options: {
                cutout: '70%',
                plugins: { 
                    legend: { display: false },
                    tooltip: {
                        enabled: chartData.channel_data && chartData.channel_data.length > 0
                    }
                },
                responsive: true,
                maintainAspectRatio: false,
            }
        });
    }
}
