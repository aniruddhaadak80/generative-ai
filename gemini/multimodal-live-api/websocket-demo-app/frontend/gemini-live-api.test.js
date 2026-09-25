const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class FakeWebSocket {
    constructor(url) {
        this.url = url;
        this.messages = [];
        this.closed = false;
    }

    send(message) {
        this.messages.push(message);
    }

    close() {
        this.closed = true;
    }
}

function loadApi() {
    const source = fs.readFileSync(
        path.join(__dirname, "gemini-live-api.js"),
        "utf8",
    );
    const context = {
        WebSocket: FakeWebSocket,
        alert() {},
        console: { log() {} },
    };
    vm.runInNewContext(
        `${source}\nthis.GeminiLiveAPI = GeminiLiveAPI;`,
        context,
    );
    return context.GeminiLiveAPI;
}

test("uses the same websocket for setup and teardown", () => {
    const GeminiLiveAPI = loadApi();
    const api = new GeminiLiveAPI(
        "ws://localhost:8080",
        "project",
        "model",
        "host",
    );

    api.setupWebSocketToService();
    const websocket = api.websocket;
    websocket.onopen();
    api.disconnect();

    assert.ok(websocket);
    assert.equal(websocket.url, "ws://localhost:8080");
    assert.equal(websocket.messages.length, 2);
    assert.equal(websocket.closed, true);
    assert.equal(api.websocket, null);
});

test("sends through the active websocket", () => {
    const GeminiLiveAPI = loadApi();
    const api = new GeminiLiveAPI(
        "ws://localhost:8080",
        "project",
        "model",
        "host",
    );
    const messages = [];
    api.websocket = {
        send(message) {
            messages.push(message);
        },
    };

    api.sendMessage({ type: "test" });

    assert.deepEqual(messages, [JSON.stringify({ type: "test" })]);
});

test("disconnect closes and clears the active websocket", () => {
    const GeminiLiveAPI = loadApi();
    const api = new GeminiLiveAPI(
        "ws://localhost:8080",
        "project",
        "model",
        "host",
    );
    let closed = false;
    api.websocket = {
        close() {
            closed = true;
        },
    };

    api.disconnect();
    api.disconnect();

    assert.equal(closed, true);
    assert.equal(api.websocket, null);
});

test("send after disconnect is ignored", () => {
    const GeminiLiveAPI = loadApi();
    const api = new GeminiLiveAPI(
        "ws://localhost:8080",
        "project",
        "model",
        "host",
    );
    api.disconnect();

    assert.doesNotThrow(() => api.sendMessage({ type: "test" }));
});
