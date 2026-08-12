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

const PROTO = process.env.PTSL_PROTO_PATH ||
  'C:\\ProTools\\PTSLSDK\\PTSL_SDK_CPP.2026.04.0.1301892\\Source\\PTSL.proto';
const ADDR = process.env.PTSL_ADDR || 'localhost:31416';

const CMD = {
  GetTrackList: 3,
  SetPlaybackMode: 32,
  SetRecordMode: 33,
  GetSessionSampleRate: 35,
  GetTransportState: 59,
  TogglePlayState: 64,
  ToggleRecordEnable: 65,
  GetMemoryLocations: 69,
  RegisterConnection: 70,
  CreateMemoryLocation: 71,
  CreateNewTracks: 72,
  SelectTracksByName: 73,
  SetTimelineSelection: 81,
  SetTrackMuteState: 85,
  SetTrackSoloState: 86,
  SetTrackRecordEnableState: 88,
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

async function main() {
  const cmd = process.argv[2];
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
    case 'markers': {
      const r = await send(CMD.GetMemoryLocations, { page_limit: 200 });
      const list = r.memory_locations || [];
      console.log(`${list.length} memory location(s):`);
      for (const m of list) {
        console.log(`  #${String(m.number || '').padEnd(4)} ${(m.name || '').padEnd(24)} ${m.start_time || ''}`);
      }
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
