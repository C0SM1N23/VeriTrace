import { expect, type APIRequestContext } from "@playwright/test";

/** Follow the asynchronous production lifecycle before issuing dependent REST calls. */
export async function waitForReady(request: APIRequestContext, backend: string, sid: string) {
  await expect.poll(async () => {
    const response = await request.get(`${backend}/session/${sid}/status`);
    expect(response.ok(), await response.text()).toBeTruthy();
    const status = await response.json();
    if (status.phase === "error") throw new Error(status.error);
    return status.phase;
  }, { timeout: 30_000 }).toBe("ready");
}
