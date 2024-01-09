import logging
import os
from pathlib import Path
from textwrap import dedent

import git
import yaml
from github import Github, GithubException, UnknownObjectException
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Center, Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    LoadingIndicator,
    Markdown,
    Static,
    Switch,
)

from nf_core.pipelines.create.utils import TextInput

log = logging.getLogger(__name__)

github_text_markdown = """
# Create a GitHub repo

After creating the pipeline template locally, we can create a GitHub repository and push the code to it.
"""
repo_config_markdown = """
Please select the the GitHub repository settings:
"""


class GithubRepo(Screen):
    """Create a GitHub repository and push all branches."""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()
        yield Markdown(dedent(github_text_markdown))
        with Horizontal():
            gh_user, gh_token = self._get_github_credentials()
            yield TextInput(
                "gh_username",
                "GitHub username",
                "Your GitHub username",
                default=gh_user[0] if gh_user is not None else "GitHub username",
                classes="column",
            )
            yield TextInput(
                "token",
                "GitHub token",
                "Your GitHub personal access token for login.",
                default=gh_token if gh_token is not None else "GitHub token",
                password=True,
                classes="column",
            )
            yield Button("Show", id="show_password")
            yield Button("Hide", id="hide_password")
        yield Markdown(dedent(repo_config_markdown))
        with Horizontal():
            yield Switch(value=False, id="private")
            yield Static("Select if the new GitHub repo must be private.", classes="custom_grid")
        with Horizontal():
            yield Switch(value=True, id="push")
            yield Static(
                "Select if you would like to push all the pipeline template files to your GitHub repo\nand all the branches required to keep the pipeline up to date with new releases of nf-core.",
                classes="custom_grid",
            )
        yield Center(
            Button("Create GitHub repo", id="create_github", variant="success"),
            Button("Finish without creating a repo", id="exit", variant="primary"),
            classes="cta",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Create a GitHub repo or show help message and exit"""
        if event.button.id == "show_password":
            self.add_class("displayed")
            text_input = self.query_one("#token", TextInput)
            text_input.query_one(Input).password = False
        elif event.button.id == "hide_password":
            self.remove_class("displayed")
            text_input = self.query_one("#token", TextInput)
            text_input.query_one(Input).password = True
        elif event.button.id == "create_github":
            # Create a GitHub repo

            # Save GitHub username and token
            github_variables = {}
            for text_input in self.query("TextInput"):
                this_input = text_input.query_one(Input)
                github_variables[text_input.field_id] = this_input.value
            # Save GitHub repo config
            for switch_input in self.query("Switch"):
                github_variables[switch_input.id] = switch_input.value

            # Pipeline git repo
            pipeline_repo = git.Repo.init(
                Path(self.parent.TEMPLATE_CONFIG.outdir)
                / Path(self.parent.TEMPLATE_CONFIG.org + "-" + self.parent.TEMPLATE_CONFIG.name)
            )

            # GitHub authentication
            if github_variables["token"]:
                github_auth = self._github_authentication(github_variables["gh_username"], github_variables["token"])
            else:
                raise UserWarning(
                    f"Could not authenticate to GitHub with user name '{github_variables['gh_username']}'."
                    "Please provide an authentication token or set the environment variable 'GITHUB_AUTH_TOKEN'."
                )

            user = github_auth.get_user()
            org = None
            # Make sure that the authentication was successful
            try:
                user.login
                log.debug("GitHub authentication successful")
            except GithubException:
                raise UserWarning(
                    f"Could not authenticate to GitHub with user name '{github_variables['gh_username']}'."
                    "Please make sure that the provided user name and token are correct."
                )

            # Check if organisation exists
            # If the organisation is nf-core or it doesn't exist, the repo will be created in the user account
            if self.parent.TEMPLATE_CONFIG.org != "nf-core":
                try:
                    org = github_auth.get_organization(self.parent.TEMPLATE_CONFIG.org)
                    log.info(
                        f"Repo will be created in the GitHub organisation account '{self.parent.TEMPLATE_CONFIG.org}'"
                    )
                except UnknownObjectException:
                    pass

            # Create the repo
            try:
                if org:
                    self._create_repo_and_push(
                        org, pipeline_repo, github_variables["private"], github_variables["push"]
                    )
                    self.screen.loading = True
                else:
                    # Create the repo in the user's account
                    log.info(
                        f"Repo will be created in the GitHub organisation account '{github_variables['gh_username']}'"
                    )
                    self._create_repo_and_push(
                        user, pipeline_repo, github_variables["private"], github_variables["push"]
                    )
                    self.screen.loading = True
                log.info(f"GitHub repository '{self.parent.TEMPLATE_CONFIG.name}' created successfully")
            except UserWarning as e:
                log.info(f"There was an error with message: {e}")
                self.parent.switch_screen("github_exit")

    class RepoCreated(Message):
        """Custom message to indicate that the GitHub repo has been created."""

        pass

    @on(RepoCreated)
    def stop_loading(self) -> None:
        self.screen.loading = False
        self.parent.switch_screen("completed_screen")

    @work(thread=True, exclusive=True)
    def _create_repo_and_push(self, org, pipeline_repo, private, push):
        """Create a GitHub repository and push all branches."""
        self.query_one(LoadingIndicator).border_title = "Creating GitHub repo..."
        # Check if repo already exists
        try:
            repo = org.get_repo(self.parent.TEMPLATE_CONFIG.name)
            # Check if it has a commit history
            try:
                repo.get_commits().totalCount
                raise UserWarning(f"GitHub repository '{self.parent.TEMPLATE_CONFIG.name}' already exists")
            except GithubException:
                # Repo is empty
                repo_exists = True
            except UserWarning as e:
                # Repo already exists
                self.post_message(self.RepoCreated())
                log.info(e)
                return
        except UnknownObjectException:
            # Repo doesn't exist
            repo_exists = False

        # Create the repo
        if not repo_exists:
            repo = org.create_repo(
                self.parent.TEMPLATE_CONFIG.name, description=self.parent.TEMPLATE_CONFIG.description, private=private
            )

        # Add the remote and push
        try:
            pipeline_repo.create_remote("origin", repo.clone_url)
        except git.exc.GitCommandError:
            # Remote already exists
            pass
        if push:
            pipeline_repo.remotes.origin.push(all=True).raise_if_error()

        self.post_message(self.RepoCreated())

    def _github_authentication(self, gh_username, gh_token):
        """Authenticate to GitHub"""
        log.debug(f"Authenticating GitHub as {gh_username}")
        github_auth = Github(gh_username, gh_token)
        return github_auth

    def _get_github_credentials(self):
        """Get GitHub credentials"""
        gh_user = None
        gh_token = None
        # Use gh CLI config if installed
        gh_cli_config_fn = os.path.expanduser("~/.config/gh/hosts.yml")
        if os.path.exists(gh_cli_config_fn):
            with open(gh_cli_config_fn) as fh:
                gh_cli_config = yaml.safe_load(fh)
                gh_user = (gh_cli_config["github.com"]["user"],)
                gh_token = gh_cli_config["github.com"]["oauth_token"]
        # If gh CLI not installed, try to get credentials from environment variables
        elif os.environ.get("GITHUB_TOKEN") is not None:
            gh_token = self.auth = os.environ["GITHUB_TOKEN"]
        return (gh_user, gh_token)
