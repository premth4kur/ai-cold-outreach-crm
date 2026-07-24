Place your Google service-account JSON key file in this folder.

1. Google Cloud Console -> create a project (or reuse one).
2. Enable "Google Sheets API" and "Google Drive API".
3. IAM & Admin -> Service Accounts -> Create service account.
4. Create a key (JSON) and download it.
5. Save it here as:  service_account.json
6. Open your CRM Google Sheet -> Share -> add the service account's
   email address (looks like  name@project.iam.gserviceaccount.com )
   as an Editor.

This folder is git-ignored. Never commit the JSON key.
