// Test JavaScript file
console.log('Static files working!');

// Add event listeners when document is ready
document.addEventListener('DOMContentLoaded', function() {
    console.log('Document ready!');
    
    // Initialize Socket.IO with configuration
    const socket = io({
        transports: ['polling', 'websocket'],
        upgrade: true,
        rememberUpgrade: true,
        path: '/socket.io/',
        reconnection: true,
        reconnectionAttempts: 5,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        timeout: 20000,
        autoConnect: true,
        forceNew: true,
        withCredentials: false,
        host: 'localhost',
        port: 5000
    });

    // Socket.IO event handlers
    socket.on('connect', () => {
        console.log('Connected to WebSocket server');
        updateConnectionStatus(true);
    });

    socket.on('disconnect', () => {
        console.log('Disconnected from WebSocket server');
        updateConnectionStatus(false);
    });

    socket.on('connect_error', (error) => {
        console.error('WebSocket connection error:', error);
        updateConnectionStatus(false);
    });

    socket.on('error', (error) => {
        console.error('WebSocket error:', error);
        updateConnectionStatus(false);
    });

    // Function to update connection status
    function updateConnectionStatus(connected) {
        const statusElement = document.getElementById('connection-status');
        if (statusElement) {
            statusElement.textContent = connected ? 'Connected' : 'Disconnected';
            statusElement.className = connected ? 'text-success' : 'text-danger';
        }
    }

    // Test error handling
    window.onerror = function(msg, url, lineNo, columnNo, error) {
        console.error('Error: ' + msg + '\nURL: ' + url + '\nLine: ' + lineNo + '\nColumn: ' + columnNo + '\nError object: ' + JSON.stringify(error));
        return false;
    };
}); 