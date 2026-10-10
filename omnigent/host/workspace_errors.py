"""Safe workspace preparation failures shared by the launcher and host."""

GITHUB_CHECKOUT_UNCONNECTED_EXIT = 81
GITHUB_CHECKOUT_UNCONNECTED = "github_checkout_unconnected"
GITHUB_CHECKOUT_MACHINE_UNAVAILABLE_EXIT = 85
GITHUB_CHECKOUT_MACHINE_UNAVAILABLE = "github_checkout_machine_unavailable"
WORKSPACE_ERROR_MESSAGES = {
    GITHUB_CHECKOUT_MACHINE_UNAVAILABLE: (
        "GitHub checkout failed: GitHub App access is not authorized for this machine identity. "
        "Ask an administrator to configure github_machine_auth for the bot, "
        "verify its identity is enabled and the App is installed on the repository, "
        "then start a new session."
    ),
    GITHUB_CHECKOUT_UNCONNECTED: (
        "GitHub checkout failed without a connected GitHub account. "
        "For a private repository, connect GitHub in Settings > Integrations. "
        "Check the repository URL and branch, then start a new session."
    ),
}
