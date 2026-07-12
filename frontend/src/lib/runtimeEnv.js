export const PYTHON_VERSIONS = ["3.13", "3.12", "3.11", "3.10", "3.9"];

export const OS_OPTIONS = [
  { value: "linux", label: "Linux" },
  { value: "windows", label: "Windows" },
  { value: "darwin", label: "macOS" },
];

export function buildRuntimeEnvironment(pythonVersion, os) {
  if (os === "windows") {
    return {
      python_version: pythonVersion,
      sys_platform: "win32",
      os_name: "nt",
      platform_machine: "x86_64",
      platform_system: "Windows",
      implementation_name: "cpython",
    };
  }

  if (os === "darwin") {
    return {
      python_version: pythonVersion,
      sys_platform: "darwin",
      os_name: "posix",
      platform_machine: "x86_64",
      platform_system: "Darwin",
      implementation_name: "cpython",
    };
  }

  return {
    python_version: pythonVersion,
    sys_platform: "linux",
    os_name: "posix",
    platform_machine: "x86_64",
    platform_system: "Linux",
    implementation_name: "cpython",
  };
}
