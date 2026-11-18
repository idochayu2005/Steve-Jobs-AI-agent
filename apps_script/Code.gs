/**
 * Webhook for the Steve Jobs agent, bound to the "Jobs" Google Sheet.
 *
 * Setup: Extensions > Apps Script in the sheet, paste this file, add the
 * Script property SHEET_SECRET, then Deploy > New deployment > Web app
 * (Execute as: Me, Who has access: Anyone). After changing this file:
 * Deploy > Manage deployments > edit > Version: New version > Deploy.
 *
 * Every request is a POST with JSON {"secret": "...", "action": "...", ...}.
 * Actions:
 *   (none) / "append"  {rows: [[...16 values]]} -> {ok, added, skipped}
 *   "alerts"           {days} -> {ok, messages:[{id,date,from,subject,body}]}
 *                      Gmail threads labelled "Job Alerts" (read only).
 *   "replies"          {days} -> {ok, messages:[...]} possible answers from
 *                      companies to applications (read only).
 *   "trash"            {ids:[...]} -> {ok, trashed, skipped}
 *                      Moves processed alert emails to Trash (recoverable
 *                      for 30 days). Only messages labelled "Job Alerts"
 *                      are touched; anything else is skipped.
 *   "sent"             {days} -> {ok, messages:[{id,threadId,date,to,subject,
 *                      body,pdf}]} applications Ido sent (CV attached or
 *                      apply/CV wording); mail only to his own addresses is
 *                      left out (read only).
 *   "rows"             -> {ok, rows:[{row, company, title, status, link}]}
 *   "set_status"       {updates:[{row, status, expect, note}]}
 *                      -> {ok, updated, skipped}
 *
 * Columns A-P: Company | Job title | Type | City | Years required | Degree |
 * Key tech | AI fit (0-100) | What's missing | Link | Found | Status | Notes |
 * Tailored resume | Personal note | Source
 *
 * Gmail: nothing is sent or labelled. The only change is "trash", which
 * moves already-processed "Job Alerts" emails to Trash.
 */
var TAB_NAME = 'Jobs';
var LINK_COLUMN = 10;   // J
var STATUS_COLUMN = 12; // L
var NOTES_COLUMN = 13;  // M
var SOURCE_COLUMN = 16; // P
var COLUMN_COUNT = 16;
var ALERT_LABEL = 'Job Alerts';
var STATUSES = ['New', 'Applied', 'Interview', 'Offer', 'Rejected', 'Not interested'];
var MAX_BODY = 20000;

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    var body = JSON.parse(e.postData.contents);
    var expected = PropertiesService.getScriptProperties().getProperty('SHEET_SECRET');
    if (!expected || body.secret !== expected) {
      return reply_({ok: false, error: 'unauthorized'});
    }
    var action = body.action || 'append';
    if (action === 'alerts') return reply_(readAlerts_(body.days));
    if (action === 'replies') return reply_(readReplies_(body.days));
    if (action === 'trash') return reply_(trashAlerts_(body.ids || []));
    if (action === 'sent') return reply_(readSent_(body.days));

    lock.waitLock(30000);
    var sheet = SpreadsheetApp.getActive().getSheetByName(TAB_NAME);
    if (!sheet) return reply_({ok: false, error: 'tab not found: ' + TAB_NAME});
    if (action === 'append') return reply_(appendRows_(sheet, body.rows || []));
    if (action === 'rows') return reply_(listRows_(sheet));
    if (action === 'set_status') return reply_(setStatus_(sheet, body.updates || []));
    return reply_({ok: false, error: 'unknown action: ' + action});
  } catch (err) {
    return reply_({ok: false, error: String(err)});
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
}

function appendRows_(sheet, rows) {
  if (!sheet.getRange(1, SOURCE_COLUMN).getValue()) {
    sheet.getRange(1, SOURCE_COLUMN).setValue('Source');
  }
  var lastRow = sheet.getLastRow();
  var known = {};
  if (lastRow > 1) {
    sheet.getRange(2, LINK_COLUMN, lastRow - 1, 1).getValues().forEach(function (r) {
      if (r[0]) known[String(r[0]).trim()] = true;
    });
  }
  var newRows = [];
  var skipped = 0;
  rows.forEach(function (row) {
    var link = String(row[LINK_COLUMN - 1] || '').trim();
    if (!link || known[link]) { skipped++; return; }
    known[link] = true;
    var padded = row.slice(0, COLUMN_COUNT);
    while (padded.length < COLUMN_COUNT) padded.push('');
    newRows.push(padded);
  });
  if (newRows.length) {
    sheet.getRange(lastRow + 1, 1, newRows.length, COLUMN_COUNT).setValues(newRows);
  }
  return {ok: true, added: newRows.length, skipped: skipped};
}

function listRows_(sheet) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return {ok: true, rows: []};
  var values = sheet.getRange(2, 1, lastRow - 1, STATUS_COLUMN).getValues();
  var rows = values.map(function (v, i) {
    return {row: i + 2, company: String(v[0]), title: String(v[1]),
            link: String(v[LINK_COLUMN - 1]), status: String(v[STATUS_COLUMN - 1])};
  });
  return {ok: true, rows: rows};
}

function setStatus_(sheet, updates) {
  var updated = 0, skipped = 0;
  updates.forEach(function (u) {
    var row = Number(u.row);
    if (!row || row < 2 || STATUSES.indexOf(u.status) < 0) { skipped++; return; }
    var cell = sheet.getRange(row, STATUS_COLUMN);
    var current = String(cell.getValue());
    // Only change the row if it still has the status the agent saw.
    if (u.expect !== undefined && current !== String(u.expect)) { skipped++; return; }
    cell.setValue(u.status);
    if (u.note) {
      var notes = sheet.getRange(row, NOTES_COLUMN);
      var old = String(notes.getValue());
      notes.setValue(old ? old + ' | ' + u.note : u.note);
    }
    updated++;
  });
  return {ok: true, updated: updated, skipped: skipped};
}

function readAlerts_(days) {
  var query = 'label:"' + ALERT_LABEL + '" newer_than:' + safeDays_(days) + 'd';
  return {ok: true, messages: readMessages_(query, 50)};
}

function readReplies_(days) {
  var query = 'newer_than:' + safeDays_(days) + 'd -label:"' + ALERT_LABEL + '" ' +
      '-from:jobalerts-noreply@linkedin.com -from:jobs-listings@linkedin.com ' +
      '-from:me ("thank you for applying" OR "thanks for applying" OR "application was sent" ' +
      'OR "received your application" OR "your application" OR "application received" ' +
      'OR "thank you for your application" OR "thanks for your application" ' +
      'OR "move forward" ' +
      'OR "moving forward" OR "other candidates" OR "not to proceed" ' +
      'OR "unfortunately" OR "interview" OR "קורות החיים" OR "מועמדותך" OR "מועמדות" ' +
      'OR "תודה על פנייתך" OR "קיבלנו את")';
  return {ok: true, messages: readMessages_(query, 50)};
}

function trashAlerts_(ids) {
  var trashed = 0, skipped = 0;
  ids.forEach(function (id) {
    try {
      var message = GmailApp.getMessageById(String(id));
      var labelled = message && message.getThread().getLabels().some(function (l) {
        return l.getName() === ALERT_LABEL;
      });
      if (!labelled) { skipped++; return; }
      message.moveToTrash();
      trashed++;
    } catch (err) {
      skipped++;
    }
  });
  return {ok: true, trashed: trashed, skipped: skipped};
}

function readSent_(days) {
  var query = 'in:sent newer_than:' + safeDays_(days) + 'd ' +
      '(filename:pdf OR "קורות חיים" OR "קורות החיים" OR "מועמדות" OR cv OR resume ' +
      'OR "apply" OR "applying" OR "application" OR "position")';
  var own = [Session.getActiveUser().getEmail()].concat(GmailApp.getAliases())
      .map(function (a) { return String(a).toLowerCase(); });
  var out = [];
  GmailApp.search(query, 0, 50).forEach(function (thread) {
    thread.getMessages().forEach(function (m) {
      var from = String(m.getFrom()).toLowerCase();
      var isMine = own.some(function (a) { return a && from.indexOf(a) >= 0; });
      if (!isMine) return;
      var to = [m.getTo(), m.getCc()].join(',').split(',')
          .map(function (x) { return x.trim(); })
          .filter(function (x) {
            var lower = x.toLowerCase();
            return x && !own.some(function (a) { return a && lower.indexOf(a) >= 0; });
          });
      if (!to.length) return;  // only to Ido's own addresses
      var pdf = m.getAttachments().some(function (a) {
        return /\.pdf$/i.test(a.getName());
      });
      out.push({id: m.getId(), threadId: thread.getId(), date: m.getDate().toISOString(),
                to: to.join(', '), subject: m.getSubject(), pdf: pdf,
                body: String(m.getPlainBody()).slice(0, 4000)});
    });
  });
  return {ok: true, messages: out};
}

function readMessages_(query, maxThreads) {
  var out = [];
  GmailApp.search(query, 0, maxThreads).forEach(function (thread) {
    thread.getMessages().forEach(function (m) {
      out.push({id: m.getId(), date: m.getDate().toISOString(), from: m.getFrom(),
                subject: m.getSubject(), body: String(m.getPlainBody()).slice(0, MAX_BODY)});
    });
  });
  return out;
}

function safeDays_(days) {
  var d = Math.floor(Number(days) || 3);
  return Math.min(Math.max(d, 1), 60);
}

/** Run once from the editor to grant access to the sheet and Gmail. */
function authorize() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(TAB_NAME);
  var threads = GmailApp.search('label:"' + ALERT_LABEL + '"', 0, 1);
  Logger.log('Access OK. Rows in ' + TAB_NAME + ': ' + (sheet ? sheet.getLastRow() : 'tab not found') +
             '. Threads with label "' + ALERT_LABEL + '": ' + threads.length);
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
