# Instagram Graph API Setup Guide

This guide walks you through connecting the **Music Video Pipeline Manager** to the Meta Instagram Graph API so you can automatically publish 9:16 vertical videos (YouTube Shorts) as **Instagram Reels**.

---

## Prerequisites

Before starting, ensure you have:
1. **A Meta Developer Account**: Register at [developers.facebook.com](https://developers.facebook.com/).
2. **An Instagram Professional Account**:
   - Meta requires an Instagram **Business** or **Creator** account for API publishing (Personal accounts cannot publish via API).
   - In the Instagram mobile app: go to **Settings & Privacy** > **Account type and tools** > **Switch to professional account** (it's completely free).
3. **A Facebook Page connected to your Instagram Account**:
   - Meta connects Instagram accounts to the Graph API through a linked Facebook Page.
   - On desktop, go to your Facebook Page > **Settings** > **Linked Accounts** > **Instagram** > **Connect Account**.

---

## Step 1: Create or Configure Your Meta Developer App

If you already created a Meta App for Facebook page posting, you can use the same App ID and App Secret! Just make sure the Instagram permissions and redirect URI are configured.

If creating a new Meta App:
1. Go to [Meta for Developers](https://developers.facebook.com/apps/) and click **Create App**.
2. Select **Other** as the use case, click **Next**.
3. Select **Business** as the app type, click **Next**.
4. Enter an **App name** (e.g. `Music Video Pipeline`) and your contact email, then click **Create App**.

---

## Step 2: Add Instagram & Facebook Login Products

1. In your App Dashboard left sidebar, find **Add Products** (or **Use cases**).
2. Add **Facebook Login for Business** (or **Facebook Login**).
3. Under **Facebook Login** > **Settings**:
   - Find **Valid OAuth Redirect URIs**.
   - Enter:
     ```text
     http://localhost:5055/
     ```
   - Click **Save Changes**.

---

## Step 3: Retrieve App ID & App Secret

1. In the left sidebar, navigate to **App settings** > **Basic**.
2. Copy your **App ID**.
3. Click **Show** next to **App Secret** (enter your password if prompted) and copy your **App Secret**.

---

## Step 4: 1-Click In-App Authorization

The Pipeline Manager features a fully automated OAuth authorization flow (just like YouTube):

1. Launch the **Music Video Pipeline Manager** (`python run.py`).
2. Click **⚙ Settings** in the top navigation bar.
3. Switch to the **Instagram Integration** tab.
4. Paste your **Meta App ID** and **Meta App Secret** (if you already entered them in the Facebook tab, they will auto-fill).
5. Ensure the Redirect URI is set to:
   ```text
   http://localhost:5055/
   ```
6. Click **Authorize Instagram**:
   - A local callback server will automatically spin up on port 5055.
   - Your default web browser will open to Meta's authorization screen.
   - Log in with your Facebook account that manages your Page and linked Instagram account.
   - Select your Facebook Page and linked Instagram Business Account.
   - Grant the requested publishing permissions (`instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`, `business_management`).
   - Click **Done**.
7. The browser will display:
   ```text
   Instagram authorization successful! You can close this window and return to the app.
   ```
8. The app will automatically:
   - Capture the authorization code.
   - Exchange it for a **60-day Long-Lived Access Token**.
   - Discover your linked Instagram Account ID and Username.
   - Populate the settings and update the status badge to **Authorized ✅ (@your_handle)**.
9. Click **Test Connection** to confirm your account is active and verified.
10. Click **Save Settings**.

---

## Step 5: Configuring Default Reel Caption Template

1. In **⚙ Settings**, go to the **Default Templates** tab.
2. Scroll to **Default Instagram Reel Caption Template**.
3. The default template is:
   ```text
   {{body}}

   Title: {{title}}

   To watch the full-length 4K video, follow the link in bio. 💤✨

   #music #sleep #relaxing #reels #ambient

   © {{year}} Music To Sleep To. All Rights Reserved.
   Made with the help of Suno.
   ```
4. You can customize this template anytime using any of the standard placeholders:
   - `{{title}}`: Project / Song title
   - `{{body}}`: AI description / quote
   - `{{quote}}`: Project quote
   - `{{pill_badge}}`: Thumbnail pill badge text
   - `{{youtube-url}}`: Full-length YouTube video link
   - `{{year}}`, `{{month}}`, `{{date}}`: Current timestamp values

---

## Step 6: Publishing Instagram Reels

1. In the **Project Setup & Files** tab, enable the **Process Instagram Reel** toggle slider.
2. The pipeline will automatically use the high-quality vertical 9:16 video generated for the YouTube Short:
   ```text
   {Project Folder}/{Name} - YouTube Short.mp4
   ```
3. In the **Publishing / Deployment** tab:
   - Scroll to **Section 6: Instagram Reel Details**.
   - Review or customize the caption for this specific track.
   - (Optional) Set an ISO schedule time or leave blank for immediate publishing.
4. When you click **Deploy Project**:
   - The app uploads the video to Meta's Instagram Reel container.
   - Monitors upload processing until Meta status reports `FINISHED`.
   - Publishes the Reel directly to your Instagram profile.
   - Updates the status badge to **Done (Uploaded) ✅** and logs the Meta Media ID.
