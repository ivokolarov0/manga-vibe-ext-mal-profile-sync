# MyAnimeList sync for MangaVibe

A [MangaVibe](https://github.com/ivokolarov0/manga-vibe) plugin that keeps your MyAnimeList manga list
in step with what you read. It only syncs the **status** (Reading / Completed), not chapter counts.

| You… | MyAnimeList gets |
|---|---|
| start reading a series | **Reading** |
| finish the last book you have, and the series has **ended** | **Completed** |
| finish the last book you have, and the series is **still coming out** | stays **Reading** |
| set a series to Completed by hand | **Completed** |

**Sync all** (Settings → Plugins) and **Sync with MyAnimeList** (series page, Plugin tools) also go the
other way:

- Completed on MyAnimeList → **Completed** in MangaVibe (no matter which chapters you've read)
- Reading on MyAnimeList → **Reading** in MangaVibe, if you hadn't started it there
- anything you're reading or finished in MangaVibe that MyAnimeList doesn't have yet is sent there

A series that is Completed on MyAnimeList is never set back to Reading. On hold, Dropped and
Plan to read are never changed.

Series need a MyAnimeList id. They get one when they're matched through AniList (most are). Series
without one are skipped; the last sync result in Settings says how many.

Needs MangaVibe with plugin API v2.

## Install

1. Download [`mal_profile_sync.py`](mal_profile_sync.py) (open it on GitHub → **Download raw file**).
2. Put it in the `plugins` folder of MangaVibe's data folder (`docker-data/plugins` next to your
   `docker-compose.yml`, or `docker/mangavibe/plugins` on a Synology).
3. In MangaVibe: **Settings → Plugins → Reload plugins**. *MyAnimeList sync* appears in the list.

To update, replace the file and press Reload plugins again. To remove it, delete the file.

## Connect your MyAnimeList account

1. Open MangaVibe's **Settings → Plugins**. Under *MyAnimeList sync* it shows the **redirect URL**,
   for example `http://192.168.1.50:8080/api/plugins/mal-profile-sync/web/callback`. It's built from the
   address you opened MangaVibe at, so open it the way you normally do.
2. Go to <https://myanimelist.net/apiconfig> → **Create ID** and fill in:
   - **App Type:** `other`
   - **App Redirect URL:** the redirect URL from step 1, exactly
   - the other fields (name, description, homepage…) can be anything
3. Copy the **Client ID** into the *Client ID* box in MangaVibe. (Leave *Client secret* empty. It's
   only needed if you picked App Type `web`.)
4. Press **Connect MyAnimeList**, log in on MyAnimeList and allow access. You land back on a page that
   says "Connected". Settings now shows *Connected as &lt;your name&gt;*.
5. Press **Sync all** once to bring both sides in line.

The login renews itself. You only connect again if you press Disconnect or don't use MangaVibe for
a few months.

**If your NAS gets a new IP address**, syncing keeps working. Only a new **Connect** needs the redirect
URL on MyAnimeList to match. Give the NAS a fixed address in your router (*DHCP reservation*) and
you won't have to think about it.

## Development

Tests run against a MangaVibe checkout, by default `../../manga-vibe` (set `MANGAVIBE_DIR` otherwise),
with a fake MyAnimeList:

```
pip install -r ../../manga-vibe/requirements-dev.txt
python -m pytest
```
