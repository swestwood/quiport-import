# Exporting Quip

Exports a Quip account to a self-contained folder of HTML that lives on your computer and looks nice when opened in your browser.

Handles big imports that get rate limited. Can be stopped and resumed midway through if needed.

Resulting import includes the original folder structure, a search bar, filters, "Recents" list, inline comments, images, etc.

## How to use

- Download this repo, then open it on the command line: 
    - `cd quiport-wrapper/`
- Get your Quip API token
    - Visit [https://quip.com/dev/token](https://quip.com/dev/token) in your browser while logged in. Copy the long bit of text it gives you.
- Run this command: 
    - `python3 -m quiport check-updates --export-id myaccount --token "MYQUIPTOKEN"`
    - "myaccount" can be replaced by whatever you want to call the export
    - Replace "MYQUIPTOKEN" with your token. Make sure it in quote marks.
- Wait for it to finish a bunch of verbose API checks. Then it should say that everything is ready.
- Run this command:
    - `python3 -m quiport all --export-id myaccount --token "MYQUIPTOKEN"`
    - Replace "myaccount" and "MYQUIPTOKEN" like before
    - Then wait! It will give you updates along the way. It may take awhile depending on how big your account is.
    - It can be stopped at any point, just do Ctrl-C. To resume where you left off, just run the command again.
- When it finishes:
    - Open `quiport-wrapper/export-myaccount/index.html` in a browser to see all your imported files. 
- Sanity check
    - Run `python3 -m quiport verify --export-id myaccount --token "MYQUIPTOKEN"` to do a verification check.

## Imported folder tree

<img width="877" height="608" alt="folder-tree" src="https://github.com/user-attachments/assets/2d9f5e41-ee64-40b1-bbc2-1f41e9b0f7a5" />

## Imported document

<img width="882" height="873" alt="quiport-document" src="https://github.com/user-attachments/assets/9d4275e2-f6ff-4029-9aaa-2bb0d6b76415" />

## Another imported documend

<img width="791" height="733" alt="another-example" src="https://github.com/user-attachments/assets/dd0f1244-aa4b-49e1-a46f-30a966d37467" />


## What it supports

- Imports all documents, chats, spreadsheets, and slide decks as HTML
- Copies over Quip styling (mostly)
- Shows inline comment bubbles, with the chat messages at the bottom. Sidebar chat messages are also at the bottom.
- Reproduces folder structure and folder colors
- Fetches images and sharing info
- Shows task lists like they appear in Quip
- A search bar to search across your imported file titles
- Updates document mentions to point to the exported documents, not the quip.com links
- Code blocks show up as code blocks, with inline formatting
- Downloads every spreadsheet and slide deck in XLS and PPT format, as well as the raw HTML


## Fetching updates

If you keep using your Quip account after import, you can pick up the changes by running:

`python3 -m quiport check-updates --export-id myaccount --token "MYQUIPTOKEN`

Sample output:
```
Checked 136 threads.

Modified since this export (5):
  Team Sync  (MRYAAASRpNK)
      Jun 7, 2019 at 8:29 pm -> Aug 16, 2026 at 11:22 pm
  ...

Not yet in this export (1):
  https://acme.quip.com/WenyAqjbX6S3  (BDXAAAWaS40)  [text_document]

Refresh these 6 document(s)? [y/N]
```

Or, you can refetch a specific document with:
- `python3 -m quiport refresh --export-id myaccount "https://acme.quip.com/S8VnAlAOXYaD/Some-document" --token "MYQUIPTOKEN"`

## Post-import checks

After the import, open `export-myaccount/needs-attention.html` in your browser to see everything the import could not carry over faithfully, such as:
- Documents with missing images
- Documents with inline files
- Documents where no content was returned
- A list of spreadsheets (formulas can't be imported, only raw values)
- etc.

It will give you links to open the import and the original side by side. Most of them are probably actually fine, but just to be paranoid.

## Caveats

Everything except this readme was written with Claude Code to import my own account and almost all of the code is completely unreviewed by humans. I ran through a bunch of tests and iterations with it on edge cases and stability and to make it look okay, but I do not endorse it beyond that it worked great for importing 1500+ documents and 100+ folders on my personal account.

Some safety things built in:
- It will only read out of your Quip account, it never writes anything.
- Everything runs on your computer and is stored on your computer.
- It caches the results of every API call on disk under `export-myaccount/raw` so that the raw data is all there later if needed.
