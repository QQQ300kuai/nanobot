import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountLogin } from "@/components/AccountLogin";
import { AccountSettingsView } from "@/components/account/AccountSettingsView";
import { UsageSection } from "@/components/account/UsageSection";
import { resolveBootConnection } from "@/lib/boot-connection";
import type { BootstrapResponse } from "@/lib/types";
import { AccountProvider } from "@/providers/AccountProvider";
import { ClientProvider } from "@/providers/ClientProvider";
import type { NanobotClient } from "@/lib/nanobot-client";

import "@/i18n";

function reply(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

function stubFetch(routes: Record<string, (init?: RequestInit) => Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const route = routes[String(input)];
    return route ? route(init) : reply({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const usage = {
  total_tokens_30d: 1234,
  requests_30d: 7,
  failed_requests_30d: 1,
  cache_read_rate_30d: null,
  providers_30d: [],
};

describe("account login", () => {
  it("hands the token from POST /auth/login to the app", async () => {
    const fetchMock = stubFetch({
      "/auth/login": () => reply({ token: "acct-token" }),
    });
    const onToken = vi.fn();
    render(<AccountLogin onToken={onToken} />);

    fireEvent.change(screen.getByLabelText("User ID"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(onToken).toHaveBeenCalledWith("acct-token"));
    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ user_id: "alice", password: "pw" });
  });

  it("shows the rejection and stays on the form", async () => {
    stubFetch({ "/auth/login": () => reply({ error: "invalid_credentials" }, 401) });
    const onToken = vi.fn();
    render(<AccountLogin onToken={onToken} />);

    fireEvent.change(screen.getByLabelText("User ID"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "bad" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Incorrect user ID or password")).toBeTruthy();
    expect(onToken).not.toHaveBeenCalled();
  });
});

describe("websocket ticket", () => {
  const boot = {
    multi_user: true,
    token: "",
    ws_path: "/",
  } as unknown as BootstrapResponse;

  it("connects with a fresh one-shot ticket on every call", async () => {
    let issued = 0;
    const fetchMock = stubFetch({
      "/auth/ws-ticket": () => reply({ ticket: `ticket-${++issued}` }),
    });

    const first = await resolveBootConnection(boot, "acct-token");
    const second = await resolveBootConnection(boot, "acct-token");

    expect(first.url).toContain("?ticket=ticket-1");
    expect(second.url).toContain("?ticket=ticket-2");
    expect(first.url).not.toContain("acct-token");
    expect(first.apiToken).toBe("acct-token");
    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect(init.method).toBe("POST");
  });
});

describe("account settings", () => {
  function renderView(isAdmin: boolean, onLogout = vi.fn()) {
    stubFetch({
      "/api/usage": () => reply(usage),
      "/api/mcp/catalog": () => reply({ servers: [] }),
    });
    render(
      <ClientProvider client={{} as NanobotClient} token="acct-token" allowUpdateFlow={false}>
        <AccountProvider value={{ userId: "alice", isAdmin, replaceToken: vi.fn() }}>
          <AccountSettingsView onBackToChat={vi.fn()} onLogout={onLogout} />
        </AccountProvider>
      </ClientProvider>,
    );
    return onLogout;
  }

  it("offers only account tools to a non-admin and signs out", async () => {
    const onLogout = renderView(false);

    expect(screen.getByText("alice")).toBeTruthy();
    expect(screen.queryByText("Models")).toBeNull();
    expect(screen.queryByText("Channels")).toBeNull();
    expect(await screen.findByText("1.2K")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Sign out" })[0]!);
    expect(onLogout).toHaveBeenCalled();
  });
});

describe("usage page", () => {
  it("shows the per-user summary to administrators only", async () => {
    const fetchMock = stubFetch({
      "/api/usage": () => reply(usage),
      "/api/admin/usage": () => reply({
        ...usage,
        users_30d: [{ user_id: "bob", total_tokens: 500, requests: 3, failed_requests: 0 }],
      }),
    });

    const { unmount } = render(<UsageSection token="t" isAdmin={false} />);
    expect(await screen.findByText("1.2K")).toBeTruthy();
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).not.toContain("/api/admin/usage");
    unmount();

    render(<UsageSection token="t" isAdmin />);
    expect(await screen.findByText("bob")).toBeTruthy();
  });
});
