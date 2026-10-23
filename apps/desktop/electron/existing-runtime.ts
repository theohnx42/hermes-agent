/** Background features may use a runtime, but may not install one. */
export function requireExistingRuntime<T extends { bootstrap: boolean }>(backend: T): T {
  if (backend.bootstrap) {
    throw new Error(
      'A usable local Hermes runtime is required for this operation. Connect to a running gateway or open setup explicitly; this request will not install Hermes.'
    )
  }

  return backend
}
