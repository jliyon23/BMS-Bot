from __future__ import annotations

WELCOME = """👋 <b>Welcome to BMS Watch</b>

I watch BookMyShow for you and send an alert the moment something changes for the movies you care about.

Send me your <b>6-digit PIN code</b> to find your city and start."""

ASK_PIN = """📍 Send your <b>6-digit PIN code</b>.

Example: <code>500081</code>"""

INVALID_PIN = "Please send a valid 6-digit PIN code."

PIN_NOT_FOUND = """😕 I could not map that PIN code to a BookMyShow region.

Use /region &lt;city&gt; to set it manually, for example:
<code>/region hyderabad</code>"""

CHOOSE_REGION = "📍 <b>Select your region</b>"

NO_REGIONS = """😕 Region data is unavailable right now.

Please try again in a minute, or use /region &lt;city&gt;."""

CHOOSE_MOVIE = """🎬 <b>Now showing in {region}</b>

Select a movie to watch."""

NO_MOVIES = """😕 I could not load the movie list for {region} right now.

Try again with /movies, or use /movie &lt;name&gt;."""

ALERTS_HEADER = """🎯 <b>{movie}</b>

Choose the alerts you want. Tap a row to turn it on or off."""

SUBSCRIBED = """✅ <b>You're watching {movie}</b>

Region: {region}
I'll check every 10 seconds and message you when something changes.

Use /watches to manage this subscription."""

WATCHES_HEADER = "👀 <b>Your watches</b>"

NO_WATCHES = """You are not watching anything yet.

Use /start to add a movie."""

WATCH_REMOVED = "🗑️ Watch removed."

SETTINGS = """⚙️ <b>Settings</b>

Region: <b>{region}</b>
PIN: <code>{pin}</code>

Change region: <code>/region &lt;city&gt;</code>
Remove my data: /delete"""

DELETED = "✅ Your account and watches have been removed."

HELP = """Use these commands:

/start — add a movie
/movies — browse movies in your region
/movie &lt;name&gt; — search for a movie by name
/watches — manage subscriptions and alerts
/region &lt;city&gt; — change region
/help — alert explanations
/delete — remove your data"""

ERROR_NO_PROXY = """⚠️ The scraper has no working proxy right now.

Your request was not sent directly to BookMyShow. Please try again shortly."""
