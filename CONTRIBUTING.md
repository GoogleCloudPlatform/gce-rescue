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

> *Strive for simplicity while keeping a balance between code readability and
efficiency.*

### Commit message

We aim to adopt the Conventional Commits specification to improve readability,
clarity, and collaboration.

https://www.conventionalcommits.org/en/v1.0.0/#specification

### Branch name

Similar to commit naming, there are various methods to name branches. Let's
adhere to these simple template in our project:

```
<category/reference/description-in-kebab-case>
```

- **`category`** distinguishes the types of activities in the branches. Options
are:
  - `feature` - adding a new feature or refactoring existing code
  - `bugfix` - fixing an issue/bug (i.e. unplanned changes)
  - `test` - experimenting, PoC, etc.

- **`reference`** points to the source of info about the purpose of this branch.
Current options are:
  - `issue-123` - GitHub issue number
  - `b-1234567` - Buganizer ID *(for now, only for Googlers)*
  - `no-ref` - when there is no reference *(e.g. for `test` branches)*

- **`description-in-kebab-case`** of the problem. Try to keep a balance between
  brevity and informativeness.

### Issue / Pull request content

Several useful templates for new issues and pull requests will be provided
automatically. Although not mandatory, adhering to them helps standardize the
review process.

All Pull Requests, with rare exceptions *(such as docs, minor wording edits,
etc.), should use the `develop` branch as a base. Only team members have
permissions to push to the `main` branch.

### Code style

We strive to adhere the standard [PEP8](https://peps.python.org/pep-0008/)
document for our code. Utilize tools like
[`pycodestyle`](https://pypi.org/project/pycodestyle/) *(formerly `pep8`)* or
[`black`](https://pypi.org/project/black/) for syntax compliance checks.
Additionally, consider [`pylint`](https://pypi.org/project/pylint/) with the
standard configuration for more comprehensive syntax analysis.
