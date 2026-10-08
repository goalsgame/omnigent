"""Safe workspace preparation failures shared by the launcher and host."""

GITHUB_CHECKOUT_UNCONNECTED_EXIT = 81
GITHUB_CHECKOUT_UNCONNECTED = "github_checkout_unconnected"
WORKSPACE_ERROR_MESSAGES = {
    GITHUB_CHECKOUT_UNCONNECTED: (
        "GitHub checkout failed without a connected GitHub account. "
        "For a private repository, connect GitHub in Settings > Integrations. "
        "Check the repository URL and branch, then start a new session."
    ),
}
