    # SigmaOS-setup

    Post-install setup (omamac-style)

    Part of the **SigmaOS** sovereign operating-system ecosystem — a multi-repository
    distribution modelled on Linux Mint and Omarchy Linux. See the parent
    [`SigmaOS`](https://github.com/AaryanSinghChauhan09/SigmaOS) repository for the
    full component index.

    ## Upstream inspirations
    - https://github.com/omacom/omamac

    ## Design principles applied
    - **Separation of Concerns**
- **Declarative Manifests**
- **Defensive Programming**
- **Fail-Fast**
- **KISS**
- **Deterministic Testing**

    ## Usage
    Verbs: `steps-plan, packages-plan, dotfiles-plan, verify`

    ```
    $ sigma-setup steps-plan steps.json
1. [ ] locale     Set locale (UTC + UTF-8)
2. [ ] dots       Link dotfiles (optional)
$ sigma-setup packages-plan packages.json --backend pacman
$ sigma-setup dotfiles-plan dotfiles.json --root "$HOME"
$ sigma-setup verify checks.json --root "$HOME"

    ```

    ## Build & test
    ```
    make build
    make test
    ```

    ## Licence
    GNU General Public License v3.0 (FOSS).
