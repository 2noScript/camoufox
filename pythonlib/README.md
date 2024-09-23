<div align="center">

# Camoufox Python Interface

#### Lightweight wrapper around the Playwright API to help launch Camoufox.

</div>

---

## Installation

First, install the `camoufox` package:

```bash
pip install -U camoufox
```

Then, download the Camoufox browser:

**Windows**

```bash
camoufox fetch
```

**MacOS & Linux**

```bash
python3 -m camoufox fetch
```

To uninstall, run `camoufox remove`.

<details>
<summary>CLI options</summary>

```
Usage: python -m camoufox [OPTIONS] COMMAND [ARGS]...

Options:
  --help  Show this message and exit.

Commands:
  fetch    Fetch the latest version of Camoufox
  remove   Remove all downloaded files
  test     Open the Playwright inspector
  version  Display the current version
```

</details>

<hr width=50>

## Usage

Camoufox is fully compatible with your existing Playwright code. You only have to change your browser initialization:

#### Sync API

```python
from camoufox.sync_api import Camoufox

with Camoufox(headless=False) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
```

#### Async API

```python
from camoufox.async_api import AsyncCamoufox

async with AsyncCamoufox(headless=False) as browser:
    page = await browser.new_page()
    await page.goto("https://example.com")
```

<details>
<summary>Parameters</summary>

```
Launches a new browser instance for Camoufox.
Accepts all Playwright Firefox launch options, along with the following:

Parameters:
    config (Optional[Dict[str, Any]]):
        Camoufox properties to use. (read https://github.com/daijro/camoufox/blob/main/README.md)
    os (Optional[ListOrString]):
        Operating system to use for the fingerprint generation.
        Can be "windows", "macos", or "linux", or a list of these to choose from randomly.
        Default: ["windows", "macos", "linux"]
    block_images (Optional[bool]):
        Whether to block all images.
    block_webrtc (Optional[bool]):
        Whether to block WebRTC entirely.
    firefox_user_prefs (Optional[Dict[str, Any]]):
        Firefox user preferences to set.
    addons (Optional[List[str]]):
        List of Firefox addons to use. 
    fingerprint (Optional[Fingerprint]):
        BrowserForge fingerprint to use.
        If not provided, a random fingerprint will be generated based on the provided os & user_agent.
    exclude_addons (Optional[List[DefaultAddons]]):
        Default addons to exclude. Passed as a list of camoufox.DefaultAddons enums.
    user_agent (Optional[ListOrString]):
        User agent to use for the fingerprint generation. Either a string or a list of strings.
        Note: This must be a valid BrowserForge User-Agent string.
              To use a different user agent, set the "navigator.userAgent" preference in `config`.
    fonts (Optional[List[str]]):
        Fonts to load into Camoufox (in addition to the default fonts for the target `os`).
        Takes a list of font family names that are installed on the system.
    args (Optional[List[str]]):
        Arguments to pass to the browser.
    env (Optional[Dict[str, Union[str, float, bool]]]):
        Environment variables to set.
    executable_path (Optional[str]):
        Custom Camoufox browser executable path.
    **launch_options (Dict[str, Any]):
        Additional Firefox launch options.
```

</details>

---

### Config

Camoufox [config data](https://github.com/daijro/camoufox?tab=readme-ov-file#fingerprint-injection) can be passed as a dictionary to the `config` parameter:

```python
from camoufox.sync_api import Camoufox

with Camoufox(
    config={
        'webrtc:ipv4': '123.45.67.89',
        'webrtc:ipv6': 'e791:d37a:88f6:48d1:2cad:2667:4582:1d6d',
    }
) as browser:
    page = browser.new_page()
    page.goto("https://www.browserscan.net/webrtc")
```

<hr width=50>

### BrowserForge Integration

Camoufox is compatible with [BrowserForge](https://github.com/daijro/browserforge) fingerprints.

By default, Camoufox will use a random fingerprint. You can also inject your own Firefox Browserforge fingerprint into Camoufox with the following example:

```python
from camoufox.sync_api import Camoufox
from browserforge.fingerprints import FingerprintGenerator

fg = FingerprintGenerator(browser='firefox')

# Launch Camoufox with a random Firefox fingerprint
with Camoufox(fingerprint=fg.generate()) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
```

<hr width=50>

**Note:** As of now, some properties from BrowserForge fingerprints will not be passed to Camoufox. This is due to the outdated fingerprint dataset from Apify's fingerprint-suite (see [here](https://github.com/apify/fingerprint-suite/discussions/308)). Properties will be re-enabled as soon as an updated dataset is available.

---