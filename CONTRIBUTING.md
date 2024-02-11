# How to Contribute

We'd love to accept your patches and contributions to this project. There are
just a few small guidelines you need to follow.

## Contributor License Agreement

Contributions to this project must be accompanied by a Contributor License
Agreement (CLA). You (or your employer) retain the copyright to your
contribution; this simply gives us permission to use and redistribute your
contributions as part of the project. Head over to
<https://cla.developers.google.com/> to see your current agreements on file or
to sign a new one.

You generally only need to submit a CLA once, so if you've already submitted one
(even if it was for a different project), you probably don't need to do it
again.

## Code Reviews

All submissions, including submissions by project members, require review. We
use GitHub pull requests for this purpose. Consult
[GitHub Help](https://help.github.com/articles/about-pull-requests/) for more
information on using pull requests.

## Community Guidelines

This project follows
[Google's Open Source Community Guidelines](https://opensource.google/conduct/).

## Consistency standards

### Commit message

We're trying to utilise the Conventional Commits specification. Try to follow
this unofficial agreement to enhance readability, clarity, and collaboration.

https://www.conventionalcommits.org/en/v1.0.0/#specification

### Branch name

As well as with commit naming, there are a lot of ways to name a branches.
Let's try to follow these great guides to organise our project:

- https://dev.to/couchcamote/git-branching-name-convention-cch
- https://dev.to/varbsan/a-simplified-convention-for-naming-branches-and-commits-in-git-il4

Branch name template:
```
<category/reference/description-in-kebab-case>
```

- `category` distinguishes the types of activities in the branches. Options are:
  - `feature` - for adding new features or refactoring existing code
  - `bugfix` - for fixing an issue/bug (unplanned changes)
  - `test` - for experimenting, PoC, etc.

- `reference` points to the source of info about the purpose of this branch.
  Current options are:
  - `issue-123` - GitHub issue number
  - `b-1234567` - Buganizer ID *(for now, only for Googlers)*
  - `no-ref` - when there is no reference *(e.g. for `test` branches)*

- `description-in-kebab-case` of the problem. Try to keep a balance between
  brevity and informative.

### Issue / Pull request content

There are useful templates for a new issues and pull requests which will be
offered automatically. While you're not obligated to follow them, we recommend
that you do so in order to standardize the review process.

### Code style

We strive to use the standard [PEP8](https://peps.python.org/pep-0008/) document
for our code. You can utilize various tools to check syntax compliance, such as
[`pycodestyle`](https://pypi.org/project/pycodestyle/) *(former `pep8`)* or
[`black`](https://pypi.org/project/black/), as well as
[`pylint`](https://pypi.org/project/pylint/) with the standard configuration,
which can also be extremely useful *(sometimes too much)*.

Despite being one of the most important yet ambiguous sections, the message is
as follows:

**Try to be as simple as possible while balancing code readability and
efficiency.**
