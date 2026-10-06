import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ signOut: vi.fn(), client: {} }));
vi.mock("firebase/auth", () => ({ signOut: (...args: unknown[]) => mocks.signOut(...args) }));
vi.mock("@/app/firebase", () => ({ auth: mocks.client }));

import { AppSidebar } from "@/chrome/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";

function sidebar() {
  render(
    <MemoryRouter>
      <SidebarProvider>
        <AppSidebar
          section={null}
          entries={null}
          local={null}
          servedRole={null}
          searchFor={() => ""}
          subject={null}
          caseId={null}
        />
      </SidebarProvider>
    </MemoryRouter>,
  );
}

describe("AppSidebar sign out with a Firebase client", () => {
  it("signs out through the client", async () => {
    mocks.signOut.mockResolvedValue(undefined);
    sidebar();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(mocks.signOut).toHaveBeenCalledWith(mocks.client));
  });

  it("swallows a failed sign-out without an unhandled rejection", async () => {
    mocks.signOut.mockRejectedValue(new Error("network"));
    sidebar();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(mocks.signOut).toHaveBeenCalledTimes(2));
  });
});
