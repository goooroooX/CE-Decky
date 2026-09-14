const assert = require('node:assert/strict');
const proton = require(process.env.CE_DECKY_TS_OUT + '/steam/proton.js');
const client = require(process.env.CE_DECKY_TS_OUT + '/steam/client.js');
const tableImport = require(process.env.CE_DECKY_TS_OUT + '/tableImport.js');


{
  const member = { path: 'table.CT', size: 1, packed_size: 1, encrypted: false, format: 'ct' };
  assert.equal(tableImport.canAutoImportTableMember([member]), true);
  // One encrypted member is auto-imported exactly once, so the backend can try
  // the password the provider published beside the artifact; after that attempt
  // the user has to supply one and the manual row takes over.
  assert.equal(tableImport.canAutoImportTableMember([{ ...member, encrypted: true }]), true);
  assert.equal(tableImport.canAutoImportTableMember([{ ...member, encrypted: true }], true), false);
  assert.equal(tableImport.canAutoImportTableMember([member], true), true);
  assert.equal(tableImport.canAutoImportTableMember([member, { ...member, path: 'other.CT' }]), false);
  assert.equal(tableImport.canAutoImportTableMember([]), false);

  const failed = { error: 'archive member requires a password' };
  assert.equal(tableImport.passwordPromptRequired({ error: null }, false), false);
  assert.equal(tableImport.passwordPromptRequired({ error: null }, true), true);
  assert.equal(tableImport.passwordPromptRequired(failed, false), true);
  assert.equal(tableImport.passwordPromptRequired({ error: 'HTTP 403' }, false), false);
}

{
  assert.equal(proton.classifyProton({appId:1,isShortcut:false,compatToolName:'proton_9',compatToolDisplayName:'Proton 9',compatToolPriority:0,platforms:['windows']}).state, 'confirmed_proton');
  assert.equal(proton.classifyProton({appId:1,isShortcut:false,compatToolName:'GE-Proton9-27',compatToolDisplayName:'',compatToolPriority:0,platforms:['windows']}).state, 'confirmed_proton');
  assert.equal(proton.classifyProton({appId:1,isShortcut:false,compatToolName:'proton-ge-9',compatToolDisplayName:'',compatToolPriority:0,platforms:['windows']}).state, 'confirmed_proton');
  for (const compatToolName of ['not-proton', 'protontricks', 'ge-custom-runtime']) {
    assert.equal(proton.classifyProton({appId:1,isShortcut:false,compatToolName,compatToolDisplayName:'',compatToolPriority:0,platforms:['windows']}).state, 'uncertain');
  }
  assert.equal(proton.classifyProton({appId:1,isShortcut:false,compatToolName:'',compatToolDisplayName:'',compatToolPriority:0,platforms:['windows']}).state, 'compatibility_required');
  assert.equal(proton.classifyProton({appId:1,isShortcut:false,compatToolName:'',compatToolDisplayName:'',compatToolPriority:0,platforms:['linux']}).state, 'native_linux');
  assert.equal(proton.classifyProton({appId:1,isShortcut:true,compatToolName:'',compatToolDisplayName:'',compatToolPriority:0,platforms:[]}).state, 'uncertain');
}

(async () => {
  delete global.SteamClient;
  await assert.rejects(() => client.readAppDetails(10, 100), /SteamClient Apps API is unavailable/);

  let unregistered = 0;
  global.window = {
    appStore: { allApps: [{ appid: 9001, app_type: 1 << 30, display_name: 'Shortcut', sort_as: 'Shortcut' }] },
  };
  global.SteamClient = {
    Apps: {
      RegisterForAppDetails(appId, callback) {
        callback({
          unAppID: appId, strDisplayName: 'Game', strShortcutExe: appId === 9001 ? '/x.exe' : '',
          strCompatToolName: 'proton_9', strCompatToolDisplayName: 'Proton 9', nCompatToolPriority: 1,
          vecPlatforms: ['windows'],
        });
        return { unregister() { unregistered++; } };
      },
    },
    InstallFolder: {
      async GetInstallFolders() { return [{ vecApps: [{ nAppID: 10, strAppName: 'Steam Game', strSortAs: 'Steam Game' }] }]; },
    },
  };

  // CE Decky reads Steam identity and never writes it. A Steam client that
  // exposes no launch-option setter at all must still satisfy every read path.
  assert.equal(global.SteamClient.Apps.SetAppLaunchOptions, undefined);
  assert.equal(global.SteamClient.Apps.SetShortcutLaunchOptions, undefined);

  const originalRegister = global.SteamClient.Apps.RegisterForAppDetails;
  global.SteamClient.Apps.RegisterForAppDetails = (appId, callback) => {
    callback({ unAppID: appId + 1 });
    return { unregister() { unregistered++; } };
  };
  await assert.rejects(() => client.readAppDetails(10, 100), /identity mismatch/);
  global.SteamClient.Apps.RegisterForAppDetails = originalRegister;

  await assert.rejects(() => client.readAppDetails(10, 0), /timeoutMs must be/);

  const details = await client.readAppDetails(10, 100);
  assert.equal(details.displayName, 'Game');
  assert.equal(details.isShortcut, false);
  assert.ok(unregistered >= 1, 'synchronous callback registration must still unregister');

  const shortcut = await client.readAppDetails(9001, 100);
  assert.equal(shortcut.isShortcut, true);
  assert.equal(shortcut.shortcutExe, '/x.exe');

  const games = await client.listInstalledGames();
  assert.deepEqual(games.map(x => [x.appId, x.isShortcut]), [[9001, true], [10, false]]);

  // Conflicting identity observations fail closed rather than silently turning a
  // Steam app profile into a shortcut (or vice versa) by enumeration order.
  global.window.appStore.allApps.push({ appid: 10, app_type: 1 << 30, display_name: 'Collision', sort_as: 'Collision' });
  await assert.rejects(() => client.listInstalledGames(), /identity collision/);
  global.window.appStore.allApps.pop();

  await assert.rejects(() => client.readAppDetails(0x100000000, 100), /4294967295/);

  console.log('TS core assertions: PASS');
})().catch(error => { console.error(error); process.exit(1); });
