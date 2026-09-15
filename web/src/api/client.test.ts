import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { encode, decode } from "@msgpack/msgpack";
import { WaveSocket } from "./client";

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  binaryType = "";
  onopen = () => {};
  onclose = () => {};
  onerror = () => {};
  onmessage = (_ev: { data: ArrayBuffer }) => {};
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose(); });
  constructor(_url: string) { Socket.instances.push(this); }
  open() { this.readyState = Socket.OPEN; this.onopen(); }
  chunk(h: number, done = false) {
    this.onmessage({ data: encode({ op: "wave_chunk", h, done }).slice().buffer as ArrayBuffer });
  }
}

beforeEach(() => { vi.useFakeTimers(); Socket.instances = []; vi.stubGlobal("WebSocket", Socket); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("wave connection lifecycle", () => {
  it("disposes a pending handshake without requesting data or reconnecting", async () => {
    const receive = vi.fn();
    const wave = new WaveSocket("s", receive);
    const opening = wave.connect();
    const rejected = expect(opening).rejects.toMatchObject({ name: "AbortError" });
    wave.request({ signals: [1], t0: 0, t1: 100, pxWidth: 50 });
    wave.close();
    await rejected;
    const socket = Socket.instances[0];
    expect(socket.close).not.toHaveBeenCalled();
    socket.open();
    expect(socket.close).toHaveBeenCalledOnce();
    expect(socket.send).not.toHaveBeenCalled();
    await vi.runAllTimersAsync();
    expect(Socket.instances).toHaveLength(1);
    expect(receive).not.toHaveBeenCalled();
  });

  it.each([false, true])("replays an interrupted request, preferring a newer viewport: %s", async (newer) => {
    const receive = vi.fn();
    const status = vi.fn();
    const wave = new WaveSocket("s", receive, status);
    const opening = wave.connect();
    const first = Socket.instances[0];
    first.open();
    await opening;
    const request = { signals: [1, 2], t0: 0, t1: 100, pxWidth: 50 };
    wave.request(request);
    first.chunk(1);
    if (newer) wave.request({ ...request, t0: 20 });
    first.close();
    expect(status).toHaveBeenLastCalledWith(expect.stringContaining("Reconnecting"));
    await vi.advanceTimersByTimeAsync(1000);
    const second = Socket.instances[1];
    second.open();
    expect(decode(second.send.mock.calls[0][0])).toMatchObject({ t0: newer ? 20 : 0, signals: [1, 2] });
    second.chunk(2, true);
    expect(receive).toHaveBeenCalledOnce();
    expect(receive.mock.calls[0][0]).toEqual([{ op: "wave_chunk", h: 2, done: true }]);
    expect(status).toHaveBeenLastCalledWith(null);
    wave.close();
  });
});
