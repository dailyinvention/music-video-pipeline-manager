# Privacy Policy for Music Video Pipeline Manager

**Effective Date:** August 30, 2026  
**Last Updated:** August 30, 2026  

## 1. Introduction
This Privacy Policy outlines how the **Music Video Pipeline Manager** application ("we", "our", or "the Application") accesses, uses, stores, and protects your information when interacting with the **TikTok Content Posting API / Direct Post API** and other third-party services.

The Application is a private, internal desktop workflow automation tool designed specifically for content creators to produce, manage, and publish original music videos directly to their own authorized social media accounts.

---

## 2. Information We Collect and Access
When you authorize the Application with your TikTok account via OAuth 2.0, the Application accesses only the permissions explicitly granted by you:

* **Account Identification (`user.info.basic`):** Basic account identifier (Open ID and Display Name) to verify the connected account.
* **Video Upload Permissions (`video.upload`, `video.publish`):** Authorization tokens required to initialize, upload, and publish video content directly to your TikTok account.
* **OAuth Credentials:** Access tokens and refresh tokens generated during the secure TikTok OAuth 2.0 authorization flow.

The Application **does not** collect, store, or access:
* Your TikTok password.
* Private direct messages, personal chats, or contacts.
* Financial, billing, or payment information.
* Personal data of third-party users or followers.

---

## 3. How We Use Information
Any information accessed through the TikTok API is strictly used for the following operational purposes:
1. **Video Publishing:** Uploading and scheduling your rendered video files and metadata (titles, captions, hashtags) to your authorized TikTok profile.
2. **Session Maintenance:** Using secure refresh tokens to maintain active API connection without requiring frequent manual re-logins.

---

## 4. Data Storage and Security
* **Local Storage Only:** All API keys, client secrets, access tokens, refresh tokens, and video files are stored locally on your own computer in a secure local SQLite database.
* **No External Servers:** We do not operate external web servers, cloud databases, or telemetry trackers that collect or store your credentials.
* **Direct Communication:** All network requests are made directly between your local device and official TikTok endpoints (`https://open.tiktokapis.com/`) over encrypted HTTPS (TLS).

---

## 5. Third-Party Sharing and Disclosure
* **No Data Selling:** We do not sell, rent, monetize, or trade any user data or credentials.
* **No Third-Party Analytics:** We do not include third-party advertising SDKs, data brokers, or analytics tracking services in the Application.
* **No Unauthorized Sharing:** Your tokens and data are never shared with any entity other than the official TikTok API required to perform the video upload services you initiate.

---

## 6. Data Retention and Deletion
* **Token Deletion:** You can delete or clear your stored TikTok credentials and tokens at any time directly within the Application's **Settings** menu.
* **Revoking Access:** You can revoke the Application's access to your TikTok account at any time through the TikTok mobile app or web platform (*Settings and privacy* -> *Security* -> *Manage app permissions*). Revoking access immediately invalidates all active tokens.

---

## 7. Compliance with TikTok API Terms
The Application strictly adheres to the [TikTok Developer Terms of Service](https://developers.tiktok.com/doc/developer-terms-of-service) and [TikTok Privacy Policy](https://www.tiktok.com/legal/privacy-policy).

---

## 8. Contact Information
If you have any questions, inquiries, or data requests regarding this Privacy Policy, please contact:

* **Application:** Music Video Pipeline Manager
* **Organization:** Daily Invention
* **Contact Email:** stefan@dailyinvention.com
