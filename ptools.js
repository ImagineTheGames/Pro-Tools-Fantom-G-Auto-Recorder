#!/usr/bin/env node
/**
 * ptools.js - Direct PTSL control of Pro Tools.
 *
 * Talks to the gRPC server Pro Tools exposes on localhost:31416, using the
 * PTSL.proto from Avid's SDK. Written as a CLI so the stem-capture tool can
 * drive Pro Tools' transport in step with MIDI playback.
 *
 * Usage:
 *   node ptools.js info
 *   node ptools.js tracks
 *   node ptools.js create-track --name "Fantom Stems" --format stereo
 *   node ptools.js select --name "Fantom Stems"
 *   node ptools.js record-arm
 *   node ptools.js play
 *   node ptools.js stop
 *   node ptools.js transport
 *   node ptools.js marker --name "Bass" --time 00:00:12:00
 *   node ptools.js markers-from-cues <cues.csv>
 */

import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import * as fs from 'fs';
import * as path from 'path';
import * as readline from 'readline';

const PROTO = process.env.PTSL_PROTO_PATH ||
  'C:\\ProTools\\PTSLSDK\\PTSL_SDK_CPP.2026.04.0.1301892\\Source\\PTSL.proto';
const ADDR = process.env.PTSL_ADDR || 'localhost:31416';

const CMD = {
  GetTrackList: 3,
  GetSessionPath: 43,
  SelectAllClipsOnTrack: 4,
  SetPlaybackMode: 32,
  SetRecordMode: 33,
  ExportSessionInfoAsText: 30,
  GetSessionSampleRate: 35,
  GetTransportState: 59,
  TogglePlayState: 64,
  ToggleRecordEnable: 65,
  TrimToSelection: 6,
  Copy: 21,
  RenameTargetTrack: 8,
  CreateSession: 0,
  OpenSession: 1,
  SaveSession: 18,
  GetSessionName: 42,
  GetEditModeOptions: 79,
  SetEditModeOptions: 80,
  GetEditSelection: 12,
  GetMemoryLocations: 69,
  ClearMemoryLocation: 61,
  RegisterConnection: 70,
  CreateMemoryLocation: 71,
  CreateNewTracks: 72,
  SelectTracksByName: 73,
  SetTimelineSelection: 81,
  SetTrackMuteState: 85,
  SetTrackSoloState: 86,
  SetTrackRecordEnableState: 88,
  SetEditMode: 75,
  Clear: 23,
  Cut: 20,
  Undo: 104,
  Redo: 105,
  Paste: 22,
  SetTrackInputMonitorState: 90,
  GetTransportArmed: 58,
};

let client = null;
let sessionId = '';

async function connect() {
  const def = await protoLoader.load(PROTO, {
    keepCase: true, longs: String, enums: String, defaults: true, oneofs: true,
  });
  const pkg = grpc.loadPackageDefinition(def);
  client = new pkg.ptsl.PTSL(ADDR, grpc.credentials.createInsecure());
}

function send(commandId, body = {}, skipSession = false) {
  return new Promise((resolve, reject) => {
    const header = { command: commandId, version: 2025, version_minor: 10, version_revision: 0 };
    if (sessionId && !skipSession) header.session_id = sessionId;
    client.SendGrpcRequest(
      { header, request_body_json: JSON.stringify(body) },
      (err, res) => {
        if (err) return reject(new Error(`gRPC: ${err.message}`));
        if (res.command_error_type && res.command_error_type !== 'CmdErr_NoError') {
          const detail = res.response_error_json || res.command_error_type;
          return reject(new Error(`PTSL ${res.command_error_type}: ${detail}`));
        }
        let out = {};
        if (res.response_body_json) {
          try { out = JSON.parse(res.response_body_json); } catch { out = res.response_body_json; }
        }
        resolve(out);
      }
    );
  });
}

async function register(appName = 'FantomStemCapture') {
  const r = await send(CMD.RegisterConnection,
    { company_name: 'fantom-stem', application_name: appName }, true);
  sessionId = r.session_id;
  return sessionId;
}

function arg(name, fallback = undefined) {
  const i = process.argv.indexOf('--' + name);
  if (i < 0 || i + 1 >= process.argv.length) return fallback;
  return process.argv[i + 1];
}

/**
 * Persistent mode: read one JSON command per line on stdin, write one JSON
 * result per line on stdout.
 *
 * Spawning a fresh node process per command costs 150-300ms of startup, and
 * that cost VARIES run to run. In a per-track capture that variance lands
 * between "Pro Tools started recording" and "the MIDI started", so every take
 * is offset by a different amount and the stems drift out of sync with each
 * other. Holding the connection open removes it.
 *
 *   {"cmd":"record"}                        -> {"ok":true,"state":"..."}
 *   {"cmd":"arm","name":"01 Bass"}
 *   {"cmd":"locate","samples":0}
 */
async function serve() {
  await connect();
  await register();
  process.stdout.write(JSON.stringify({ ok: true, ready: true, session: sessionId }) + "\n");

  const rl = readline.createInterface({ input: process.stdin });
  for await (const line of rl) {
    const text = line.trim();
    if (!text) continue;
    let req;
    try {
      req = JSON.parse(text);
    } catch (e) {
      process.stdout.write(JSON.stringify({ ok: false, error: "bad json" }) + "\n");
      continue;
    }
    try {
      const out = await handle(req);
      process.stdout.write(JSON.stringify(Object.assign({ ok: true }, out)) + "\n");
    } catch (e) {
      process.stdout.write(JSON.stringify({ ok: false, error: String(e.message || e) }) + "\n");
    }
    if (req.cmd === "quit") break;
  }
}

const rolling = s => s && s.current_setting &&
  s.current_setting !== "TState_TransportStopped";
const live = s => s && (s.current_setting === "TState_TransportRecording" ||
                        s.current_setting === "TState_TransportPlaying");

async function waitFor(pred, tries = 100, ms = 10) {
  let t = {};
  for (let i = 0; i < tries; i++) {
    t = await send(CMD.GetTransportState).catch(() => ({}));
    if (pred(t)) return t;
    await new Promise(r => setTimeout(r, ms));
  }
  return t;
}

/** One command, shared by serve() and the CLI. */
async function handle(req) {
  switch (req.cmd) {
    // The session FOLDER, so a capture can find its own audio files without
    // being told where they are. PTSL returns the .ptx file path.
    case "session-path": {
      const r = await send(CMD.GetSessionPath);
      const file = (r.session_path && r.session_path.path) || "";
      return { path: file, folder: file ? path.dirname(file) : "" };
    }
    case "save": {
      await send(CMD.SaveSession, {});
      return { saved: true };
    }
    // A session per song, from a template so the I/O setup comes with it --
    // which is also what keeps a stray Loopback path from being handed to a
    // newly created track.
    case "new-session": {
      // Leaving input_output_settings out defaults it to the "unknown" enum
      // value, and CreateSession then reports success and creates nothing.
      const body = {
        session_name: req.name,
        session_location: req.location,
        file_type: "FType_WAVE",
        sample_rate: "SR_" + String(req.rate || 48000),
        bit_depth: "BDepth_" + String(req.depth || 24),
        input_output_settings: req.io || "IO_Last",
        is_interleaved: req.interleaved === undefined ? true : !!req.interleaved,
        is_cloud_project: false,
        create_from_template: !!req.template,
      };
      if (req.template) {
        body.template_group = req.template_group || "";
        body.template_name = req.template;
      }
      await send(CMD.CreateSession, body);
      const n = await send(CMD.GetSessionName).catch(() => ({}));
      return { created: req.name, now_open: n.session_name || "" };
    }
    case "open-session": {
      await send(CMD.OpenSession, { session_path: req.path });
      const n = await send(CMD.GetSessionName).catch(() => ({}));
      return { now_open: n.session_name || "" };
    }
    case "ensure-track": {
      const cur = await send(CMD.GetTrackList, { page_limit: 400 });
      if ((cur.track_list || []).some(t => t.name === req.name)) return { created: false };
      await send(CMD.CreateNewTracks, {
        number_of_tracks: 1, track_name: req.name,
        track_format: req.mono ? "TFormat_Mono" : "TFormat_Stereo",
        track_type: "TType_Audio", track_timebase: "TTimebase_Samples",
      });
      return { created: true };
    }
    case "disarm-all": {
      const cur = await send(CMD.GetTrackList, { page_limit: 400 });
      const names = (cur.track_list || [])
        .filter(t => t.type === "TT_Audio" || t.type === "TType_Audio")
        .map(t => t.name);
      if (names.length) {
        await send(CMD.SetTrackRecordEnableState, { track_names: names, enabled: false });
      }
      return { disarmed: names.length };
    }
    case "arm":
      await send(CMD.SetTrackRecordEnableState,
                 { track_names: [req.name], enabled: req.off ? false : true });
      return { track: req.name };
    case "locate": {
      const s = String(req.samples === undefined ? 0 : req.samples);
      await send(CMD.SetTimelineSelection,
                 { play_start_marker_time: s, in_time: s, out_time: s });
      return { located: s };
    }
    case "record": {
      let t = await send(CMD.GetTransportState).catch(() => ({}));
      if (rolling(t)) { await send(CMD.TogglePlayState, {}); await waitFor(s => !rolling(s)); }
      let armed = await send(CMD.GetTransportArmed).catch(() => ({}));
      if (!armed.is_transport_armed) {
        await send(CMD.ToggleRecordEnable, {});
        armed = await send(CMD.GetTransportArmed).catch(() => ({}));
      }
      await send(CMD.TogglePlayState, {});
      t = await waitFor(live);
      return { state: t.current_setting || "unknown", armed: !!armed.is_transport_armed };
    }
    case "stop": {
      let t = await send(CMD.GetTransportState).catch(() => ({}));
      if (rolling(t)) { await send(CMD.TogglePlayState, {}); t = await waitFor(s => !rolling(s)); }
      return { state: t.current_setting || "unknown" };
    }
    case "marker":
      await send(CMD.CreateMemoryLocation, {
        name: req.name || "Marker",
        start_time: String(req.samples === undefined ? 0 : req.samples),
        time_properties: "TP_Marker",
        general_properties: { zoom_settings: false, pre_post_roll_times: false,
          track_visibility: false, track_heights: false, group_enables: false },
      });
      return { marker: req.name };
    case "markers": {
      // Paginated: without a limit Pro Tools returns only the first page, and a
      // partial list would make a delete look complete when it was not.
      const out = [];
      let offset = 0;
      for (;;) {
        const r = await send(CMD.GetMemoryLocations, {
          pagination_request: { limit: 100, offset },
        });
        const page = r.memory_locations || [];
        out.push(...page);
        if (page.length < 100) break;
        offset += page.length;
        if (offset > 10000) break;
      }
      return { count: out.length, markers: out.map(m => ({
        number: m.number, name: m.name, start: m.start_time,
        type: m.time_properties, track: m.track_name || "" })) };
    }
    case "clear-markers": {
      // Takes explicit numbers only. Deriving the list in here would mean this
      // command decides what to delete; the caller should have seen them first.
      const list = (req.numbers || []).map(Number).filter(n => Number.isFinite(n));
      if (!list.length) throw new Error("clear-markers: no marker numbers given");
      await send(CMD.ClearMemoryLocation, { location_list: list });
      return { cleared: list.length, numbers: list };
    }
    // Separate at the selection and keep the right-hand side. PTSL has no
    // Separate Clip command; Trim To Selection is Pro Tools' own verb for the
    // same edit -- select from the cut point to the end, and everything before
    // it goes. In Shuffle the survivor packs left to zero on its own.
    // Copy one timeline range across several tracks and paste it elsewhere,
    // keeping every track in step. Slip, never Shuffle: in Shuffle a paste
    // shoves everything after it sideways and the whole session drifts.
    case "rename-track": {
      await send(CMD.RenameTargetTrack, {
        current_name: req.name, new_name: req.to });
      return { from: req.name, to: req.to };
    }
    case "copy-range": {
      await send(CMD.SetEditMode, { edit_mode: "EMode_Slip" });
      // Without "Link Track and Edit Selection", selecting 26 tracks does not
      // widen the edit selection to cover them: the timeline range applies to
      // whichever single track already held the edit selection. Copy then
      // takes one track and Paste fills one track, silently.
      // BOTH links are needed. link_track spreads the selection across the
      // named tracks; link_timeline is what turns a timeline range into an
      // edit selection at all. With only the first, Copy/Clear act on nothing.
      await send(CMD.SetEditModeOptions, {
        edit_mode_options: {
          link_track_and_edit_selection: true,
          link_timeline_and_edit_selection: true } });
      await send(CMD.SelectTracksByName, { track_names: req.tracks });
      await send(CMD.SetTimelineSelection, {
        in_time: String(req.in), out_time: String(req.out),
        play_start_marker_time: String(req.in) });
      await send(CMD.Copy, {});
      return { copied: [req.in, req.out], tracks: req.tracks.length };
    }
    // Delete a timeline range on one track, leaving what is before it in
    // place. Slip, not Shuffle: Shuffle would pull later material backwards.
    case "clear-range": {
      await send(CMD.SetEditMode, { edit_mode: "EMode_Slip" });
      // BOTH links are needed. link_track spreads the selection across the
      // named tracks; link_timeline is what turns a timeline range into an
      // edit selection at all. With only the first, Copy/Clear act on nothing.
      await send(CMD.SetEditModeOptions, {
        edit_mode_options: {
          link_track_and_edit_selection: true,
          link_timeline_and_edit_selection: true } });
      await send(CMD.SelectTracksByName, { track_names: [req.name] });
      await send(CMD.SetTimelineSelection, {
        in_time: String(req.in), out_time: String(req.out),
        play_start_marker_time: String(req.in) });
      await send(CMD.Clear, {});
      return { track: req.name, cleared: [req.in, req.out] };
    }
    case "paste-at": {
      // BOTH links are needed. link_track spreads the selection across the
      // named tracks; link_timeline is what turns a timeline range into an
      // edit selection at all. With only the first, Copy/Clear act on nothing.
      await send(CMD.SetEditModeOptions, {
        edit_mode_options: {
          link_track_and_edit_selection: true,
          link_timeline_and_edit_selection: true } });
      await send(CMD.SelectTracksByName, { track_names: req.tracks });
      // A zero-length selection: the paste lands at the insertion and keeps
      // its own length instead of being squeezed into a highlighted range.
      await send(CMD.SetTimelineSelection, {
        in_time: String(req.at), out_time: String(req.at),
        play_start_marker_time: String(req.at) });
      await send(CMD.Paste, {});
      return { pasted_at: req.at, tracks: req.tracks.length };
    }
    case "separate-head": {
      const cut = String(req.samples === undefined ? 0 : req.samples);
      const end = String(req.end === undefined ? 0 : req.end);

      await send(CMD.SelectTracksByName, { track_names: [req.name] });
      await send(CMD.SelectAllClipsOnTrack, { track_name: req.name });
      const st = await send(CMD.GetTrackList, { page_limit: 200 });
      const holder = (st.track_list || []).find(
        t => t.track_attributes?.has_edit_selection !== "None");
      if (!holder || holder.name !== req.name)
        throw new Error(`edit selection is on '${holder?.name}', refusing to edit '${req.name}'`);

      await send(CMD.SetEditMode, { edit_mode: "EMode_Shuffle" });
      await send(CMD.SetTimelineSelection, {
        in_time: cut, out_time: end, play_start_marker_time: cut });
      await send(CMD.TrimToSelection, {});
      return { track: req.name, cut, end };
    }
    case "trim-head": {
      // Clear the silence in front of a clip. Shuffle mode makes Clear ripple
      // what follows to the left; in Slip it would leave a hole instead.
      const cut = String(req.samples === undefined ? 0 : req.samples);

      // Move the EDIT selection, not just the track selection. Cut/Clear/Paste
      // follow has_edit_selection; SelectTracksByName only sets is_selected,
      // so on its own the edit lands on whichever track held the cursor last.
      await send(CMD.SelectTracksByName, { track_names: [req.name] });
      await send(CMD.SelectAllClipsOnTrack, { track_name: req.name });

      // Refuse to cut if the edit selection is not where we think it is.
      const st = await send(CMD.GetTrackList, { page_limit: 400 });
      const holder = (st.track_list || []).find(
        t => t.track_attributes && t.track_attributes.has_edit_selection &&
             t.track_attributes.has_edit_selection !== "None");
      if (!holder || holder.name !== req.name) {
        throw new Error(`edit selection is on '${holder ? holder.name : "nothing"}', ` +
                        `refusing to cut '${req.name}'`);
      }

      await send(CMD.SetEditMode, { edit_mode: "EMode_Shuffle" });
      await send(CMD.SetTimelineSelection,
                 { play_start_marker_time: "0", in_time: "0", out_time: cut });
      await send(CMD.Clear, {});
      return { track: req.name, removed_samples: cut, verified_on: holder.name };
    }
    case "undo":
      await send(CMD.Undo, {});
      return { undone: true };
    case "redo":
      await send(CMD.Redo, {});
      return { redone: true };
    case "edit-mode":
      await send(CMD.SetEditMode, { edit_mode: req.mode || "EMode_Slip" });
      return { edit_mode: req.mode || "EMode_Slip" };
    case "tempo":
      // PTSL has no tempo setter; report what the session says it is.
      return { note: "PTSL cannot set session tempo; set it by hand" };
    case "edl": {
      // Ground truth for where clips actually sit. ExportSessionInfoAsText with
      // track EDLs returnsÃ¦Â¯Â clip's start/end per track, which is the only way
      // to confirm an edit landed -- PTSL will happily return ok for a command
      // that acted on the wrong track, and the WAV on disk never changes.
      const r = await send(CMD.ExportSessionInfoAsText, {
        include_file_list: false,
        include_clip_list: false,
        include_markers: false,
        include_plugin_list: false,
        include_track_edls: true,
        show_sub_frames: false,
        include_user_timestamps: false,
        track_list_type: "TListType_AllTracks",
        fade_handling_type: "FHType_DontShowCrossfades",
        text_as_file_format: "TFFormat_UTF8",
        output_type: "ESI_String",
        location_type: "TLType_Samples",
      });
      return { text: r.session_info || "" };
    }
    case "select-clips": {
      // Move the EDIT selection, which is what Cut/Clear/Paste actually follow.
      // Selecting the track alone only sets is_selected; has_edit_selection
      // stays wherever it was, and edits land on the wrong track.
      await send(CMD.SelectTracksByName, { track_names: [req.name] });
      await send(CMD.SelectAllClipsOnTrack, { track_name: req.name });
      return { track: req.name };
    }
    case "tracks-state": {
      const cur = await send(CMD.GetTrackList, { page_limit: 400 });
      return { tracks: (cur.track_list || []).map(t => ({
        name: t.name,
        selected: t.track_attributes && t.track_attributes.is_selected,
        edit: t.track_attributes && t.track_attributes.has_edit_selection,
      })) };
    }
    case "shift-left": {
      // Move a clip's material left by `samples`, by cutting from the transient
      // to the end and pasting at zero. Unlike Clear-in-Shuffle this doesn't
      // depend on the edit mode rippling the way we expect -- the paste lands
      // wherever we put the insertion point, which we set explicitly.
      const cut = String(req.samples === undefined ? 0 : req.samples);
      const end = String(req.end === undefined ? 0 : req.end);
      await send(CMD.SelectTracksByName, { track_names: [req.name] });
      await send(CMD.SetEditMode, { edit_mode: "EMode_Slip" });
      await send(CMD.SetTimelineSelection, { in_time: cut, out_time: end,
                                             play_start_marker_time: cut });
      await send(CMD.Cut, {});
      await send(CMD.SetTimelineSelection, { in_time: "0", out_time: "0",
                                             play_start_marker_time: "0" });
      await send(CMD.Paste, {});
      return { track: req.name, moved_by: cut };
    }
    case "clip-extent": {
      // Where does this track's material actually sit? Selecting every clip on
      // the track and reading the resulting timeline selection gives its true
      // start and end -- which is how you verify a trim, since trimming changes
      // the clip and never touches the WAV on disk.
      await send(CMD.SelectTracksByName, { track_names: [req.name] });
      await send(CMD.SelectAllClipsOnTrack, { track_name: req.name });
      const sel = await send(CMD.GetTimelineSelection,
                             { time_scale: "TScale_Samples" }).catch(() => ({}));
      return { track: req.name, in: sel.in_time, out: sel.out_time };
    }
    case "transport":
      return await send(CMD.GetTransportState);
    case "quit":
      return { bye: true };
    default:
      throw new Error("unknown cmd: " + req.cmd);
  }
}

async function main() {
  const cmd = process.argv[2];
  if (cmd === "serve") { await serve(); return; }
  if (!cmd) {
    console.error('Usage: node ptools.js <info|tracks|create-track|select|record-arm|play|stop|transport|marker|markers-from-cues>');
    process.exit(1);
  }
  if (!fs.existsSync(PROTO)) {
    console.error(`PTSL.proto not found at ${PROTO}`);
    process.exit(1);
  }

  await connect();
  await register();

  switch (cmd) {
    case 'info': {
      const rate = await send(CMD.GetSessionSampleRate).catch(e => ({ error: e.message }));
      const tr = await send(CMD.GetTrackList, { page_limit: 200 }).catch(e => ({ error: e.message }));
      const st = await send(CMD.GetTransportState).catch(e => ({ error: e.message }));
      console.log(JSON.stringify({ session_id: sessionId, sample_rate: rate, transport: st,
        track_count: (tr.track_list || []).length }, null, 2));
      break;
    }
    case 'tracks': {
      const r = await send(CMD.GetTrackList, { page_limit: 200 });
      const list = r.track_list || [];
      console.log(`${list.length} track(s):`);
      for (const t of list) {
        console.log(`  ${(t.name || '').padEnd(28)} ${t.type || ''}  ${(t.track_attributes ? JSON.stringify(t.track_attributes) : '')}`);
      }
      break;
    }
    case 'create-track': {
      const name = arg('name', 'Fantom Stems');
      const n = parseInt(arg('count', '1'), 10);
      const fmt = (arg('format', 'stereo') === 'mono') ? 'TFormat_Mono' : 'TFormat_Stereo';
      const r = await send(CMD.CreateNewTracks, {
        number_of_tracks: n,
        track_name: name,
        track_format: fmt,
        track_type: 'TType_Audio',
        track_timebase: 'TTimebase_Samples',
      });
      console.log(JSON.stringify(r, null, 2));
      break;
    }
    case 'select': {
      const name = arg('name');
      const r = await send(CMD.SelectTracksByName, { track_names: [name] });
      console.log(JSON.stringify(r, null, 2));
      break;
    }
    case 'record-arm': {
      // per-track arm; ToggleRecordEnable is the transport master button
      const name = arg('name', 'Fantom Stems');
      const on = arg('off') === undefined;
      const r = await send(CMD.SetTrackRecordEnableState, {
        track_names: [name], enabled: on,
      });
      console.log(JSON.stringify({ track: name, record_enabled: on, response: r }, null, 2));
      break;
    }
    case 'input-monitor': {
      const name = arg('name', 'Fantom Stems');
      const on = arg('off') === undefined;
      console.log(JSON.stringify(await send(CMD.SetTrackInputMonitorState, {
        track_names: [name], enabled: on,
      }), null, 2));
      break;
    }
    case 'transport-arm': {
      console.log(JSON.stringify(await send(CMD.ToggleRecordEnable, {}), null, 2));
      break;
    }
    case 'armed': {
      console.log(JSON.stringify(await send(CMD.GetTransportArmed), null, 2));
      break;
    }
    case 'play': {
      console.log(JSON.stringify(await send(CMD.TogglePlayState, {}), null, 2));
      break;
    }
    case 'record': {
      // Both TogglePlayState and ToggleRecordEnable are toggles, so every step
      // here reads state first and only acts when the state is wrong. Blind
      // toggling is what truncated takes: a stop that hadn't landed yet meant
      // the next 'record' toggled the transport OFF instead of on.
      let t = await send(CMD.GetTransportState).catch(() => ({}));
      const rolling = s => s && s.current_setting &&
        s.current_setting !== 'TState_TransportStopped';

      if (rolling(t)) {                       // left running by a previous part
        await send(CMD.TogglePlayState, {});
        for (let i = 0; i < 20; i++) {
          await new Promise(r => setTimeout(r, 50));
          t = await send(CMD.GetTransportState).catch(() => ({}));
          if (!rolling(t)) break;
        }
      }

      let armed = await send(CMD.GetTransportArmed).catch(() => ({}));
      if (!armed.is_transport_armed) {
        await send(CMD.ToggleRecordEnable, {});
        armed = await send(CMD.GetTransportArmed).catch(() => ({}));
      }
      if (!armed.is_transport_armed) {
        console.error('WARNING: transport did not arm; this would play, not record');
      }

      await send(CMD.TogglePlayState, {});
      // Wait for the transport to actually be RECORDING, not merely cued.
      // 'TState_TransportIsCued' is a staging state -- returning during it
      // means the first notes get played before the tape is moving.
      const live = s => s && (s.current_setting === 'TState_TransportRecording' ||
                             s.current_setting === 'TState_TransportPlaying');
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 50));
        t = await send(CMD.GetTransportState).catch(() => ({}));
        if (live(t)) break;
      }
      if (!live(t)) {
        console.error(`WARNING: transport is '${t.current_setting}', not recording`);
      }
      console.log(JSON.stringify({ transport_armed: !!armed.is_transport_armed,
                                   state: t.current_setting || 'unknown' }, null, 2));
      break;
    }
    case 'stop': {
      // TogglePlayState is a toggle, so firing it blind can START the transport
      // when it was already stopped. Check first, and confirm afterwards.
      let st = await send(CMD.GetTransportState).catch(() => ({}));
      const rolling = s => s && s.current_setting &&
        s.current_setting !== 'TState_TransportStopped';
      if (rolling(st)) {
        await send(CMD.TogglePlayState, {});
        for (let i = 0; i < 20; i++) {
          await new Promise(r => setTimeout(r, 50));
          st = await send(CMD.GetTransportState).catch(() => ({}));
          if (!rolling(st)) break;
        }
      }
      console.log(JSON.stringify({ state: st.current_setting || 'unknown' }, null, 2));
      break;
    }
    case 'ensure-track': {
      // create only if a track of this name isn't already there
      const name = arg('name');
      const fmt = (arg('format', 'stereo') === 'mono') ? 'TFormat_Mono' : 'TFormat_Stereo';
      const cur = await send(CMD.GetTrackList, { page_limit: 400 });
      const have = (cur.track_list || []).some(t => t.name === name);
      if (have) { console.log(JSON.stringify({ track: name, created: false })); break; }
      await send(CMD.CreateNewTracks, {
        number_of_tracks: 1, track_name: name, track_format: fmt,
        track_type: 'TType_Audio', track_timebase: 'TTimebase_Samples',
      });
      console.log(JSON.stringify({ track: name, created: true }));
      break;
    }
    case 'disarm-all': {
      const cur = await send(CMD.GetTrackList, { page_limit: 400 });
      const names = (cur.track_list || [])
        .filter(t => t.type === 'TT_Audio' || t.type === 'TType_Audio')
        .map(t => t.name);
      if (names.length) {
        await send(CMD.SetTrackRecordEnableState, { track_names: names, enabled: false });
      }
      console.log(JSON.stringify({ disarmed: names.length }));
      break;
    }
    case 'locate': {
      // Return the playhead to a sample position so every take starts together.
      const s = String(arg('samples', '0'));
      await send(CMD.SetTimelineSelection, {
        play_start_marker_time: s, in_time: s, out_time: s,
      });
      console.log(JSON.stringify({ located: s }));
      break;
    }
    case 'session-path': {
      console.log(JSON.stringify(await handle({ cmd: 'session-path' }), null, 2));
      break;
    }
    case 'markers': {
      const r = await handle({ cmd: 'markers' });
      console.log(`${r.count} memory location(s):`);
      for (const m of r.markers) {
        console.log(`  #${String(m.number).padEnd(4)} ${(m.name || '').padEnd(24)} ${m.start}`);
      }
      break;
    }
    case 'clear-markers': {
      const nums = (arg('numbers') || '').split(',').map(s => s.trim()).filter(Boolean);
      const r = await handle({ cmd: 'clear-markers', numbers: nums });
      console.log(JSON.stringify(r, null, 2));
      break;
    }
    case 'transport': {
      console.log(JSON.stringify(await send(CMD.GetTransportState), null, 2));
      break;
    }
    case 'marker': {
      // start_time is interpreted in the session's current main time format.
      // Passing a raw sample count is unambiguous; timecode strings get
      // reinterpreted against the session start and land hours away.
      const samples = arg('samples');
      const start = samples !== undefined ? String(samples) : arg('time', '0');
      const body = {
        name: arg('name', 'Marker'),
        start_time: start,
        time_properties: 'TP_Marker',
        general_properties: { zoom_settings: false, pre_post_roll_times: false,
          track_visibility: false, track_heights: false, group_enables: false },
      };
      const num = parseInt(arg('number', ''), 10);
      if (!isNaN(num)) body.number = num;
      const r = await send(CMD.CreateMemoryLocation, body);
      console.log(JSON.stringify(r, null, 2));
      break;
    }
    case 'clear-markers': {
      const r = await send(CMD.GetMemoryLocations, { page_limit: 500 });
      const list = r.memory_locations || [];
      console.log(`${list.length} marker(s) present (delete manually in Pro Tools if needed)`);
      break;
    }
    case 'markers-from-cues': {
      const csv = process.argv[3];
      if (!csv || !fs.existsSync(csv)) { console.error('cue csv not found'); process.exit(1); }
      const lines = fs.readFileSync(csv, 'utf8').trim().split(/\r?\n/);
      const head = lines[0].split(',');
      const iName = head.indexOf('part'), iKeep = head.indexOf('keep_sec');
      let made = 0;
      for (let i = 1; i < lines.length; i++) {
        const c = lines[i].split(',');
        const secs = parseFloat(c[iKeep]);
        const tc = new Date(secs * 1000).toISOString().substr(11, 8) + ':00';
        try {
          await send(CMD.CreateMemoryLocation, {
            name: c[iName], start_time: tc, time_properties: 'TP_Marker',
          });
          made++;
        } catch (e) { console.error(`  marker ${c[iName]}: ${e.message}`); }
      }
      console.log(`created ${made} marker(s)`);
      break;
    }
    default:
      console.error(`unknown command: ${cmd}`);
      process.exit(1);
  }
}

main().catch(e => { console.error(String(e.message || e)); process.exit(1); });

