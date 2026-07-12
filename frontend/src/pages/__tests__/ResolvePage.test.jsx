import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ResolvePage from "../ResolvePage";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  resolveDependencies: vi.fn(),
}));

describe("ResolvePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("submits resolve request and renders result tree", async () => {
    api.resolveDependencies.mockResolvedValue({
      input_mode: "requirements",
      hidden_roots: [],
      resolve_strategy: "resolvelib",
      total_packages: 2,
      resolve_time_ms: 85.4,
      resolved: {
        flask: {
          version: "3.1.3",
          dependencies: [
            {
              name: "werkzeug",
              constraint: ">=3.1.0",
              version: "3.1.0",
              dependency_type: "runtime",
              extra_name: null,
              environment_markers: {
                expression: 'python_version < "3.13" and extra == "speedups" and platform_system == "Linux"',
                conditions: [
                  { marker: "python_version", op: "<", value: "3.13" },
                  { marker: "extra", op: "==", value: "speedups" },
                  { marker: "platform_system", op: "==", value: "Linux" },
                ],
              },
              dependencies: [
                {
                  name: "markupsafe",
                  constraint: ">=2.1.1",
                  version: "3.0.3",
                  dependency_type: "runtime",
                  extra_name: null,
                  environment_markers: null,
                  dependencies: [],
                },
              ],
            },
          ],
        },
      },
    });

    const user = userEvent.setup();
    render(<ResolvePage />);
    await user.selectOptions(screen.getByLabelText("Python version"), "3.11");
    await user.selectOptions(screen.getByLabelText("OS"), "windows");

    await user.click(screen.getByRole("button", { name: "Resolve" }));

    expect(await screen.findByText("Result")).toBeInTheDocument();
    expect(screen.getByText(/strategy:/i)).toBeInTheDocument();
    expect(screen.getByText("flask")).toBeInTheDocument();
    expect(screen.getByText(">=3.0")).toBeInTheDocument();
    expect(screen.getByText("3.1.3")).toBeInTheDocument();
    expect(screen.getByText("werkzeug")).toBeInTheDocument();
    expect(screen.getByText("markupsafe")).toBeInTheDocument();
    expect(screen.getByText("Python < 3.13")).toBeInTheDocument();
    expect(screen.getAllByText("Linux").length).toBeGreaterThan(0);
    expect(screen.getByText("extra speedups")).toBeInTheDocument();
    expect(screen.getByText("flask[speedups]")).toBeInTheDocument();

    await waitFor(() => {
      expect(api.resolveDependencies).toHaveBeenCalled();
    });
    const payload = api.resolveDependencies.mock.calls[0][0];
    expect(payload.ecosystem).toBe("pypi");
    expect(payload.input_mode).toBe("requirements");
    expect(payload.dependencies).toEqual([
      { name: "flask", version_spec: ">=3.0", extras: [] },
      { name: "requests", version_spec: ">=2.31", extras: [] },
    ]);
    expect(payload.environment.python_version).toBe("3.11");
    expect(payload.environment.sys_platform).toBe("win32");
    expect(payload.environment.os_name).toBe("nt");
    expect(payload.environment.platform_system).toBe("Windows");
  });

  it("collapses resolve tree nodes and the whole tree", async () => {
    api.resolveDependencies.mockResolvedValue({
      input_mode: "requirements",
      hidden_roots: [],
      resolve_strategy: "resolvelib",
      total_packages: 2,
      resolve_time_ms: 85.4,
      resolved: {
        flask: {
          version: "3.1.3",
          dependencies: [
            {
              name: "werkzeug",
              constraint: ">=3.1.0",
              version: "3.1.0",
              dependency_type: "runtime",
              extra_name: null,
              environment_markers: null,
              dependencies: [
                {
                  name: "markupsafe",
                  constraint: ">=2.1.1",
                  version: "3.0.3",
                  dependency_type: "runtime",
                  extra_name: null,
                  environment_markers: null,
                  dependencies: [],
                },
              ],
            },
          ],
        },
      },
    });

    const user = userEvent.setup();
    render(<ResolvePage />);
    await user.click(screen.getByRole("button", { name: "Resolve" }));

    expect(await screen.findByText("markupsafe")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Collapse flask"));
    expect(screen.queryByText("werkzeug")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Collapse tree" }));
    expect(screen.queryByText("werkzeug")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Expand tree" }));
    expect(await screen.findByText("werkzeug")).toBeInTheDocument();
  });

  it("renders API error message", async () => {
    api.resolveDependencies.mockRejectedValue({ message: "upstream timeout" });

    const user = userEvent.setup();
    render(<ResolvePage />);
    await user.click(screen.getByRole("button", { name: "Resolve" }));

    expect(await screen.findByText("upstream timeout")).toBeInTheDocument();
  });

  it("parses dependency lines without spaces around version spec", async () => {
    api.resolveDependencies.mockResolvedValue({
      input_mode: "requirements",
      hidden_roots: [],
      resolve_strategy: "resolvelib",
      total_packages: 1,
      resolve_time_ms: 20,
      resolved: {
        "annotated-types": {
          version: "0.7.0",
          dependencies: [],
        },
      },
    });

    const user = userEvent.setup();
    render(<ResolvePage />);

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "annotated-types==0.7.0\nfastapi[all]>=0.115" } });
    await user.click(screen.getByRole("button", { name: "Resolve" }));

    await waitFor(() => {
      expect(api.resolveDependencies).toHaveBeenCalled();
    });

    expect(api.resolveDependencies.mock.calls[0][0].dependencies).toEqual([
      { name: "annotated-types", version_spec: "==0.7.0", extras: [] },
      { name: "fastapi", version_spec: ">=0.115", extras: ["all"] },
    ]);
  });

  it("hides top-level packages already covered transitively in freeze mode", async () => {
    api.resolveDependencies.mockResolvedValue({
      input_mode: "freeze",
      hidden_roots: ["annotated-types", "pydantic-core"],
      resolve_strategy: "resolvelib",
      total_packages: 3,
      resolve_time_ms: 24,
      resolved: {
        pydantic: {
          version: "2.12.5",
          latest_compatible_version: "2.13.1",
          has_newer_compatible_version: true,
          dependencies: [
            {
              name: "annotated-types",
              constraint: ">=0.6.0",
              version: "0.7.0",
              dependency_type: "runtime",
              extra_name: null,
              environment_markers: null,
              dependencies: [],
            },
            {
              name: "pydantic-core",
              constraint: "==2.41.5",
              version: "2.41.5",
              dependency_type: "runtime",
              extra_name: null,
              environment_markers: null,
              dependencies: [],
            },
          ],
        },
      },
    });

    const user = userEvent.setup();
    render(<ResolvePage />);

    await user.selectOptions(screen.getByLabelText("Input mode"), "freeze");
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "pydantic==2.12.5\nannotated-types==0.7.0\npydantic-core==2.41.5" },
    });
    await user.click(screen.getByRole("button", { name: "Resolve" }));

    expect(await screen.findByText("pydantic")).toBeInTheDocument();
    expect(screen.getAllByText("annotated-types").length).toBe(1);
    expect(screen.getAllByText("pydantic-core").length).toBe(1);
    expect(screen.getByText("newer 2.13.1")).toBeInTheDocument();

    const payload = api.resolveDependencies.mock.calls[0][0];
    expect(payload.input_mode).toBe("freeze");
  });
});
