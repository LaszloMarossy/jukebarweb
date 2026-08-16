# JukeBar Beta Tester Guide

Thank you for helping test JukeBar! This document has 9 separate test cases. **You only need to
read the ONE test case you've been asked to run — you don't need to read the others, and you
don't need any technical background.** Each test case tells you exactly what to set up, what to
click/tap, and exactly what "it worked" vs. "something's wrong" looks like.

A few words you'll see repeated in every test case, explained once here for convenience (but each
test case also explains things as it goes, so you can jump straight to yours):

- **The host device** — the phone or tablet that has the JukeBar app (iPhone) or the SpotOnJukeBar
  app (Android) installed. This device plays the actual music and is the "brain" of the whole
  system — think of it as the jukebox itself.
- **The admin page / admin browser** — a web page (opened in any browser, on any device — a
  laptop, a second phone, whatever's convenient) that lets you control the bar remotely: turn
  payment options on/off, approve or deny song requests, pause/skip, etc.
- **The customer page / customer browser** — the web page a bar patron would use on their own
  phone to browse the playlist and request songs.

If you don't already have devices/apps set up, ask whoever gave you this document before starting
— each test case assumes the JukeBar/SpotOnJukeBar app is already installed on your host device.

---

## Test Case 1: Turning a payment option on/off updates every screen, everywhere

**What this checks:** when someone flips a payment switch on one screen, every other screen —
including the phone actually playing the music — catches up and agrees, within a few seconds.

### What you'll need
- One host device (phone/tablet with JukeBar or SpotOnJukeBar installed).
- A second device (or a second browser window) to act as the **admin browser**.
- A third device (or another browser tab) to act as the **customer browser**. If you're short on
  devices, two browser tabs on the same laptop work fine for "admin browser" and "customer
  browser" — they just need to be separate tabs.

### Setup
1. On the host device, open the app and go through the setup steps:
   - When asked how customers should connect, choose **"Local + Remote"**.
   - When asked how the device itself should connect, choose **"Internet / Relay"** (the button
     will say something like **"Use Internet Mode"**).
   - Pick any playlist/music source you like.
   - Set an admin PIN (any 4–6 digit number) — write it down, you'll need it.
   - On the payments screen, make sure **"Stripe — Pay Online"** is turned **ON**.
   - Finish setup. The app should now be playing music.
2. On the host device's own screen, find the admin area (you'll need your PIN) and locate the QR
   code or web link for remote admin access. Open that link in your **admin browser**.
3. Also find the customer-facing QR code/link (shown on the main "now playing" screen) and open it
   in your **customer browser**.

### What to do
1. In the **admin browser**, find the **"Payments"** section and turn **OFF** the **"Stripe 💳"**
   toggle.
2. **Immediately** look at that same toggle again, without doing anything else. It should look
   dimmed/greyed-out for a few seconds — like it's "thinking," not just instantly flipped.
3. Wait about 10 seconds.
4. On the **host device itself** (the phone running the app), open its own admin screen — Stripe
   should now show as **OFF** there.
5. Look at the **admin browser** again — the Stripe toggle should now be un-dimmed and clearly
   show **OFF**.
6. In the **customer browser**, start a song request — there should be **no "Pay Online" option**
   offered anymore.
7. Now do the reverse: turn Stripe back **ON**, but this time do it from the **host device's own
   screen** instead of the admin browser. Confirm the admin browser and customer browser both pick
   up the change within about 10 seconds too — not just one of them.

### Success looks like
- Every screen (host device, admin browser, customer browser) agrees on Stripe's on/off state
  within about 10 seconds of any single change, regardless of which screen made the change.
- The toggle you clicked visibly looks "locked/pending" for a moment right after you click it,
  before the change is confirmed.

### Something's wrong if
- Any screen still shows the old (pre-change) state after 15+ seconds.
- The toggle you clicked never shows any "pending" appearance — it just silently changes with no
  feedback, or the browser has to be manually refreshed to see the new state.
- Turning it back on from the host device doesn't reach the browsers (i.e., it only works one
  direction).

---

## Test Case 2: A song requested at the bar can be cancelled from far away

**What this checks:** if a customer at the bar requests a song using the phone/tablet itself
(not their own device), a remote admin — even someone not physically at the bar — can cancel that
song before it plays, and it actually gets skipped, not just marked as denied somewhere while
still playing.

### What you'll need
- One host device, set up and playing music.
- A second device (or browser tab) for the **admin browser**, opened somewhere else — ideally not
  even on the same WiFi, to prove it really works "remotely."

### Setup
1. On the host device, go through setup:
   - Payments screen: turn **OFF** both **"Stripe — Pay Online"** and **"Pay to bartender"** — this
     makes every request free and instantly approved, with nothing to tap or wait for.
   - Connection: **"Internet / Relay"** (this test specifically needs the internet-based
     connection — it won't apply the same way on a WiFi-only or Hotspot-only setup).
   - Customer connection mode: either **"Local + Remote"** or **"Local Only"** both work.
   - Finish setup so it's playing music.
2. Get the remote admin link/QR from the host device and open it in your **admin browser**, on a
   different device/network if you can manage it.

### What to do
1. On the **host device itself**, tap its own **"Request"** button (not a web page — the button
   built into the phone's own now-playing screen) and request a song.
2. Confirm the song shows up in the "Up Next" list on: the host device itself, and the admin
   browser.
3. In the **admin browser** — the one that's remote/away from the bar — find that request in the
   Up Next list and tap **"Cancel"**.
4. Let the playlist continue playing and watch what happens when it would have been that song's
   turn.

### Success looks like
- The song is genuinely skipped — it never plays, even though it was requested directly from the
  bar's own device.
- It disappears from the Up Next list on both the host device and the admin browser within a few
  seconds of clicking Cancel — not just on the admin browser's own view.

### Something's wrong if
- The song still plays despite being cancelled.
- It shows as cancelled on the admin browser but still appears in Up Next on the host device (or
  vice versa).

---

## Test Case 3: Paying by card for a song, from a customer's own phone

**What this checks:** a customer paying for a song with a card (via the web page, not the bar's
own device) actually gets their song queued, played, and correctly recorded as paid — everywhere.

### What you'll need
- One host device, set up and playing music.
- A customer browser (any phone or laptop) to submit and pay for the request.
- An admin browser to watch it arrive.
- A test payment card — ask your test coordinator for one; JukeBar uses Stripe's test mode, so no
  real money is charged, but you need a valid **test** card number to complete the payment form.

### Setup
1. On the host device, go through setup:
   - Connection: **"Internet / Relay"** (this test needs the internet connection specifically —
     paying by card only works this way).
   - Customer connection mode: **"Local + Remote"** or **"Remote Only"** (NOT "Local Only" — if
     you pick Local Only, there's no customer web page to pay from at all, and this test won't be
     possible).
   - Payments screen: turn **ON** **"Stripe — Pay Online"**.
   - Ask your coordinator to confirm a Stripe test key is configured — if payment fails
     immediately with an error before you even see a card form, this is likely why, and it's a
     setup issue, not what this test is checking.
   - Finish setup so it's playing music.
2. Open the customer link (from the host device's QR code) in your customer browser.
3. Open the remote admin link in your admin browser.

### What to do
1. In the customer browser, browse the playlist, pick a song, and tap the **"💳 Pay Online"**
   button. Complete the payment form with the test card details your coordinator gave you.
2. Look at the admin browser — the request should appear in the Up Next list with a **💳** symbol
   next to it, and — importantly — with **no Approve/Deny buttons** next to it (a paid request
   never needs a bartender's approval).
3. It should also show up (even if just generically, without full detail) in the host device's own
   "up next" preview.
4. Let the song actually play all the way through.
5. Once it's done playing, check all three screens again (host device, customer browser, admin
   browser) — it should be gone from all "up next"/"currently playing" views immediately, not
   lingering on just one of them.
6. In the admin browser, find the Reports or Past Requests section and confirm the song shows up
   there, marked as paid with the 💳 symbol, with the price actually charged.

### Success looks like
- The paid request skips any approval step entirely and goes straight to Up Next.
- It disappears from every screen at essentially the same moment once it finishes playing.
- It shows up correctly in the paid history afterward.

### Something's wrong if
- The request sits waiting for someone to approve/deny it (it shouldn't — paid requests skip that
  step).
- After the song finishes, it's still showing as "up next" on one screen while gone from another.
- It's missing from the reports/history afterward, or shows the wrong price.

---

## Test Case 4: "Local Only" mode — customers can only request from the bar's own device

**What this checks:** a bar owner who's had trouble with strangers spamming song requests from
outside can switch to "Local Only" mode, which removes every way to request a song except walking
up to the bar's own device and using its Request button directly.

### What you'll need
- One host device.
- An admin browser and a customer browser, for confirming things are properly blocked.

### Setup — this test starts from scratch, during setup itself
1. On the host device, start setup fresh (or use "End Session" first if it's already set up).
2. When asked how customers should connect, choose **"Local Only"**.
3. On the payments screen, notice that **"Stripe — Pay Online"** is still shown, but it should
   look **dimmed/disabled**, with a message like *"Not usable in Local Only mode — no customer
   page exists to pay from."* Confirm you can still see it (it's not hidden), but you genuinely
   can't turn it on.
4. Turn **ON** the **"List on JukeBar map"** option (under "Discovery") when you reach that step.
5. Finish setup so it's playing music.

### What to do, part 1 — on the host device itself
1. Look at the host device's main screen — confirm there is **no QR code** shown anywhere.
2. Confirm there IS a working **"Request"** button, right on the host device's own screen.
3. Try tapping Request and submitting a song **without typing a name** — it should refuse to let
   you submit (this is required, not optional, in this mode).
4. Now submit again, this time with a name filled in.
5. Once it's approved, confirm the song shows up in the small "up next" strip that's always
   visible on screen.
6. Tap that strip to open the full list — the requester's name should be visible **only** in this
   expanded view, not in the small always-visible strip.
7. Leave the expanded view open and don't touch anything — it should close itself automatically
   after about 15 seconds.

### What to do, part 2 — confirm the bar still shows on the map
1. Open `jukebars.com/discover` in any browser (ask your coordinator for the exact address if
   different) — confirm your test bar's name, location, and current playlist show up there, even
   though its request page is locked down. These are two separate things: the map listing is not
   affected by the request lockout.

### What to do, part 3 — confirm remote request pages are properly blocked, but admin still works
1. Try opening the customer web link (from before, or generated fresh) — it should show a clear
   "not available" page, not a broken page or a page that half-works.
2. Open the admin browser and confirm you can still fully see and manage requests — approve, deny,
   everything — normally. Only the *customer* side is locked, not admin/bartender.

### Success looks like
- No QR code on-device, but the on-device Request button works and requires a name.
- The bar still appears on the public map with its playlist.
- The customer web page is clearly blocked; admin/bartender pages work completely normally.

### Something's wrong if
- A QR code is showing anywhere on the host device.
- The Request button lets you submit with a blank name.
- The bar disappears from the map entirely (it shouldn't — only requesting is blocked, not
  discovery).
- The customer web page loads normally / lets you submit a request anyway.
- Admin or bartender pages are also blocked or broken (only the 4 request-related actions should
  be blocked, nothing else).

---

## Test Case 5: Pausing requests without pausing the bar's whole vibe

**What this checks:** an operator can temporarily stop accepting new song requests (for closing
time, or when things get too chaotic) without cancelling songs already in the queue, and without
needing to fully stop the music.

### What you'll need
- One host device, already set up and playing, with at least one song already requested and
  waiting in the queue before you start this test (see step 1 below).
- An admin browser and a customer browser.

### Setup
1. Get the bar to a state where **at least one song is already sitting in "Up Next"** — submit and
   (if needed) approve one request before you begin the actual test, using whatever payment mode
   you've got configured (free is easiest). This part matters — the whole point of the test is
   confirming this song is left alone.
2. Open an admin browser and a customer browser, connected to this same bar.

### What to do
1. In the admin browser, find the **"Accepting requests"** toggle (under "Requests") and turn it
   **OFF**.
2. On the host device itself, confirm its own on-screen Request button is now hidden or disabled —
   but the screen should NOT go blank; it should still show what's currently playing and the QR
   code as normal.
3. In the customer browser, confirm the button/option to request a song is now hidden or disabled
   there too.
4. Check on the song that was already in the queue from step 1 of Setup — it should be completely
   unaffected: still playing out normally in its turn, and if it hasn't been approved/denied yet,
   a bartender can still do that normally.
5. Turn **"Accepting requests"** back **ON**.
6. Confirm the Request option reappears immediately on both the host device and the customer
   browser, with no restart or refresh needed.

### Success looks like
- New request options disappear from both the host device and customer browser while this is off.
- The already-queued song is completely unaffected the whole time.
- Everything comes back immediately when toggled back on.

### Something's wrong if
- The already-queued song stops playing, gets removed, or can no longer be approved/denied while
  this is off (it should be untouched).
- The host device's whole screen goes blank/broken instead of just hiding the Request option.
- Turning it back on requires restarting the app or refreshing before it works again.

---

## Test Case 6: Stripe showing "on" but actually behaving free, in Local Only mode

**What this checks:** a specific, tricky combination — Local Only mode, with the Stripe switch
left ON but nothing else configured for payment — should let customers request songs for free
right on the device, not silently break the Request button.

### What you'll need
- One host device.
- One admin browser (either the remote one, or just the host device's own admin screen — this
  particular test doesn't depend on which).

### Setup
1. Set up the host device with:
   - Customer connection mode: **"Local Only"**.
   - Payments: **"Stripe — Pay Online"** turned **ON**, **"Pay to bartender"** left **OFF**.
2. Finish setup so it's playing.

### What to do
1. On the host device's own screen, confirm the **"Request"** button is visible and works — this
   is the actual point of the test, since this specific combination used to hide it by mistake.
2. Submit a request using it.
3. Confirm the request auto-approves immediately — no payment screen, no waiting for anyone to
   approve it.
4. Look at the Stripe toggle, both on the host device's own admin screen and on whichever admin
   browser you're using — it should still show **ON**, just dimmed with a note like *"Not usable in
   Local Only mode."* It should not have silently flipped to OFF.
5. Changing this mode isn't a simple toggle — it requires ending the session and redoing setup.
   On the host device, use **"End Session"** in the admin area, then go through setup again,
   this time choosing **"Local + Remote"** instead of "Local Only" (keep everything else the
   same). Once it's playing again, confirm Stripe now shows as fully usable — still **ON**,
   no longer dimmed — without you having had to turn it on again from scratch.

### Success looks like
- The Request button works and requests auto-approve for free the whole time you're in Local Only
  mode with this combination.
- The Stripe toggle still visibly shows ON (just inactive) the entire time, and doesn't need to be
  re-enabled after switching modes.

### Something's wrong if
- The Request button is missing, greyed out, or doesn't work at all in this combination.
- The Stripe toggle shows OFF at any point during this test (it should stay ON throughout, just
  inactive in Local Only).

---

## Test Case 7: Music playback recovers cleanly from a Spotify hiccup (Android only)

**What this checks:** if the phone running the jukebox loses its connection to Spotify mid-set, it
should stop cleanly and lock out customers with a clear message — not keep limping along skipping
songs — and staff should be able to fix it without losing the current queue. **This test only
applies to an Android host device — there's nothing to test here on an iPhone.**

### What you'll need
- An Android host device, set up and connected to Spotify (not just local files — this test needs
  actual Spotify songs in the mix).
- An admin browser, ideally on a different network than the host device, to prove admin still
  works while the device itself is stuck.
- A way to actually interrupt Spotify — ask your coordinator how to simulate this (e.g., turning
  off the Spotify account's connection, airplane mode briefly, etc.) since this isn't something you
  can trigger from a button in the app.

### Setup
1. Set up the Android host device with:
   - Connection: **"Internet / Relay"**.
   - A playlist/queue that includes real Spotify tracks, not only downloaded local files.
2. Queue up 2 or more song requests (a mix of paid and free is ideal, but not required) so there's
   something in Up Next before you interrupt anything.
3. Open the admin browser.

### What to do
1. With everything playing normally, interrupt the Spotify connection as instructed by your
   coordinator, and let it fail **twice in a row**.
2. Confirm the host device's screen changes to a locked/blocked screen with a message like
   *"Please ask staff for assistance"* and a **"Staff"** button — and that customers genuinely
   can't do anything else on that screen.
3. While the device is locked like this, go to the **admin browser** and confirm you can still
   approve/deny other requests, or cancel one — admin should not be stuck just because the device
   itself is.
4. On the host device, tap **"Staff"**, enter the admin PIN, and either follow the on-screen
   recovery steps or use **"Re-attach to Spotify"** from the admin screen.
5. Confirm the exact song that failed picks back up — not a different, random song.
6. Confirm both of your earlier requests are still correctly sitting in Up Next across the host
   device, the customer page, and the admin browser — nothing should have vanished during the
   outage.
7. Double check all your payment/request settings are exactly as you left them — this recovery
   process should never touch any of your setup choices.

### Success looks like
- After 2 failures in a row, the device locks out customers with a clear message, but the admin
  browser keeps working the whole time.
- Recovery brings back the exact right song and doesn't lose anything from the queue.

### Something's wrong if
- The device just keeps skipping songs silently instead of locking out and asking for help.
- The admin browser also stops working while the device is locked (it shouldn't).
- After recovery, the queue is missing a request, or a different song than the one that failed
  starts playing, or any setting has changed.

---

## Test Case 8: A WiFi-only bar shows up on the public map, but gains full detail once it goes online

**What this checks:** even a bar that never connects to the internet (just a local WiFi network)
still shows up on the public "find a bar" map — just without the fancier genre-chart detail, which
only appears once the same bar switches to using the internet.

### What you'll need
- One host device with a genuine internet connection available (even though you'll set it up on
  WiFi/Hotspot mode — the device itself still needs internet access for this part).
- A browser to check `jukebars.com/discover`.
- A playlist that's a real one JukeBar already knows about (ask your coordinator for a
  recommended playlist to use) — a made-up test playlist with fake artist names won't show any
  genre detail even after going internet-only, which would make this test impossible to judge.

### Setup
1. Set up the host device using **"Bar WiFi"** or **"Android Hotspot"** connection mode (not
   Internet/Relay) — but make sure the device itself still has real internet access (e.g., WiFi
   that also reaches the internet).
2. Turn **ON** "List on JukeBar map" during setup, using the recommended playlist from your
   coordinator.
3. Finish setup so it's playing.

### What to do
1. Open `jukebars.com/discover` and find your test bar — confirm its name, location, and current
   playlist are all shown correctly.
2. Look at its little pie-chart/genre display — confirm it shows **no colors/genre data** at this
   point. This is expected, not a bug — it's the whole first half of what this test is checking.
3. Now redo setup on the same host device, this time choosing **"Internet / Relay"** instead,
   keeping "List on JukeBar map" on and the same playlist.
4. Go back to the discover page and check the same bar again after a minute or two.

### Success looks like
- The bar is visible with correct info immediately, even on WiFi-only, but with no genre coloring.
- After switching to internet mode, genre coloring appears within a couple of minutes, without
  needing to do anything else.

### Something's wrong if
- The bar doesn't show up on the map at all while on WiFi-only mode (it should).
- Genre coloring appears even while still WiFi-only (it shouldn't be possible without an internet
  connection to the relay).
- Genre coloring still hasn't appeared several minutes after switching to internet mode.

---

## Test Case 9: Turning off both payment options switches a live bar to "everything's free"

**What this checks:** if an operator turns off both ways of charging money mid-service, the bar
correctly switches to free-for-everyone mode — but only once **both** are off, not just one.

### What you'll need
- One host device, set up and playing.
- One admin browser (host device's own admin screen, remote browser, or WiFi admin page — any of
  them work for this test).

### Setup
1. Set up the host device with customer connection mode **"Local + Remote"** (not Local Only —
   this test needs Stripe to be genuinely usable, not just visible-but-inactive, so the two
   "still needs approval" checks below are meaningful).
2. On the payments screen, turn **ON** both **"Stripe — Pay Online"** and **"Pay to bartender"**.
3. Finish setup so it's playing.

### What to do
1. From your admin browser, turn **"Stripe — Pay Online"** **OFF**, but leave **"Pay to
   bartender"** **ON**.
2. Submit a new test request — confirm it still needs a bartender to tap Approve; it should **not**
   auto-approve yet (Bartender Pay alone still requires that step).
3. Now also turn **"Pay to bartender"** **OFF** (both are now off).
4. Submit another new test request — this one should auto-approve immediately, with no payment
   step and no waiting for anyone to tap anything.

### Success looks like
- With only Stripe off (Bartender Pay still on), new requests still need an explicit approval tap.
- Only once **both** are off does a new request skip straight to approved/free.

### Something's wrong if
- Turning off Stripe alone already makes new requests auto-approve (it shouldn't — Bartender Pay
  being on should still require a tap).
- Even with both off, new requests still sit waiting for approval.
