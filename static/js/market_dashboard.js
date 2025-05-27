// Initialize charts object
const charts = {};

// Initialize Socket.IO connection
const socket = io();

// Get subscribed symbols from data attribute
const container = document.querySelector('.container-fluid');
const subscribedSymbols = JSON.parse(container.dataset.subscribedSymbols);

// Handle WebSocket events
socket.on('connect', () => {
    console.log('Connected to WebSocket server');
    // Subscribe to all symbols
    subscribedSymbols.forEach(symbol => {
        socket.emit('subscribe', { symbol });
    });
});

socket.on('disconnect', () => {
    console.log('Disconnected from WebSocket server');
});

socket.on('price_update', (data) => {
    updateMarketCard(data);
    updateChart(data);
});

socket.on('trade_update', (data) => {
    // Update trade history if needed
    console.log('Trade update:', data);
});

socket.on('signal_update', (data) => {
    // Update signals if needed
    console.log('Signal update:', data);
});

socket.on('alert', (data) => {
    showAlert(data.message, data.type);
});

// Function to update market card with new data
function updateMarketCard(data) {
    const card = document.querySelector(`#card-${data.symbol}`);
    if (!card) return;

    // Update price
    const priceElement = card.querySelector('.current-price');
    if (priceElement) {
        priceElement.textContent = data.price.toFixed(2);
        priceElement.className = `current-price ${data.price_change >= 0 ? 'text-success' : 'text-danger'}`;
    }

    // Update 24h high/low
    const highElement = card.querySelector('.high-price');
    const lowElement = card.querySelector('.low-price');
    if (highElement) highElement.textContent = data.high_24h.toFixed(2);
    if (lowElement) lowElement.textContent = data.low_24h.toFixed(2);

    // Update volume
    const volumeElement = card.querySelector('.volume');
    if (volumeElement) volumeElement.textContent = data.volume.toLocaleString();
}

// Function to update chart with new data
function updateChart(data) {
    const chart = charts[data.symbol];
    if (!chart) return;

    // Add new data point
    chart.data[0].x.push(new Date());
    chart.data[0].y.push(data.price);

    // Keep only last 100 points
    if (chart.data[0].x.length > 100) {
        chart.data[0].x.shift();
        chart.data[0].y.shift();
    }

    // Update chart
    Plotly.update(chart.element, chart.data, chart.layout);
}

// Function to show alerts
function showAlert(message, type = 'info') {
    // Request notification permission
    if (Notification.permission === 'default') {
        Notification.requestPermission();
    }

    // Show browser notification if permitted
    if (Notification.permission === 'granted') {
        new Notification('Trading Alert', {
            body: message,
            icon: '/static/img/logo.png'
        });
    }

    // Show in-app notification
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    document.querySelector('.alerts-container').appendChild(alertDiv);

    // Remove after 5 seconds
    setTimeout(() => {
        alertDiv.remove();
    }, 5000);
}

// Initialize charts for each symbol
document.addEventListener('DOMContentLoaded', () => {
    subscribedSymbols.forEach(symbol => {
        const chartElement = document.querySelector(`#chart-${symbol}`);
        if (!chartElement) return;

        const chart = {
            element: chartElement,
            data: [{
                x: [],
                y: [],
                type: 'scatter',
                mode: 'lines',
                name: symbol
            }],
            layout: {
                title: `${symbol} Price Chart`,
                xaxis: { title: 'Time' },
                yaxis: { title: 'Price' }
            }
        };

        Plotly.newPlot(chartElement, chart.data, chart.layout);
        charts[symbol] = chart;
    });
}); 