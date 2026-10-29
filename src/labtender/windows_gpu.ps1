# One-shot, read-only sample of per-process GPU use on the Windows side of a WSL host.
#
# The hub runs this through powershell.exe (-EncodedCommand) over the host's own transport, so
# nothing is installed or left behind. It reads the "GPU Engine" and "GPU Process Memory"
# performance counters and maps each adapter's LUID to its PCI bus with D3DKMT calls, so the
# hub can join the rows to nvidia-smi's pci.bus_id. All WSL GPU work appears here as the single
# VM worker process (vmwp). Prints one JSON document and exits.
#
# Counter names are English; on a Windows display language without English counter names the
# script reports an error instead of rows.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class LabtenderAdapterMap
{
    [StructLayout(LayoutKind.Sequential)]
    struct ADAPTERINFO { public uint hAdapter; public uint LuidLow; public int LuidHigh;
                         public uint NumOfSources; public int bPrecisePresentRegionsPreferred; }
    [StructLayout(LayoutKind.Sequential)]
    struct ENUMADAPTERS2 { public uint NumAdapters; public IntPtr pAdapters; }
    [StructLayout(LayoutKind.Sequential)]
    struct QUERYADAPTERINFO { public uint hAdapter; public int Type; public IntPtr pPrivateDriverData;
                              public uint PrivateDriverDataSize; }
    [StructLayout(LayoutKind.Sequential)]
    struct ADAPTERADDRESS { public uint BusNumber; public uint DeviceNumber; public uint FunctionNumber; }

    [DllImport("gdi32.dll")] static extern int D3DKMTEnumAdapters2(ref ENUMADAPTERS2 p);
    [DllImport("gdi32.dll")] static extern int D3DKMTQueryAdapterInfo(ref QUERYADAPTERINFO p);
    [DllImport("gdi32.dll")] static extern int D3DKMTCloseAdapter(ref uint hAdapter);
    const int KMTQAITYPE_ADAPTERADDRESS = 6;

    // "0x{high:x8}_0x{low:x8}" (as in counter instance names) -> PCI bus number.
    public static Dictionary<string, uint> LuidToBus()
    {
        var result = new Dictionary<string, uint>();
        var e = new ENUMADAPTERS2();
        if (D3DKMTEnumAdapters2(ref e) != 0 || e.NumAdapters == 0) return result;
        int size = Marshal.SizeOf(typeof(ADAPTERINFO));
        e.pAdapters = Marshal.AllocHGlobal(size * (int)e.NumAdapters);
        try
        {
            if (D3DKMTEnumAdapters2(ref e) != 0) return result;
            for (int i = 0; i < e.NumAdapters; i++)
            {
                var info = (ADAPTERINFO)Marshal.PtrToStructure(IntPtr.Add(e.pAdapters, i * size), typeof(ADAPTERINFO));
                var addr = new ADAPTERADDRESS();
                IntPtr pAddr = Marshal.AllocHGlobal(Marshal.SizeOf(addr));
                try
                {
                    var q = new QUERYADAPTERINFO { hAdapter = info.hAdapter, Type = KMTQAITYPE_ADAPTERADDRESS,
                                                   pPrivateDriverData = pAddr, PrivateDriverDataSize = (uint)Marshal.SizeOf(addr) };
                    if (D3DKMTQueryAdapterInfo(ref q) == 0)
                    {
                        addr = (ADAPTERADDRESS)Marshal.PtrToStructure(pAddr, typeof(ADAPTERADDRESS));
                        result[string.Format("0x{0:x8}_0x{1:x8}", info.LuidHigh, info.LuidLow)] = addr.BusNumber;
                    }
                }
                finally { Marshal.FreeHGlobal(pAddr); uint h = info.hAdapter; D3DKMTCloseAdapter(ref h); }
            }
        }
        finally { Marshal.FreeHGlobal(e.pAdapters); }
        return result;
    }
}
'@

$out = [ordered]@{ schema = 1; time = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0; procs = @(); errors = @() }
try {
    $luidToBus = [LabtenderAdapterMap]::LuidToBus()
    $samples = (Get-Counter -Counter @('\GPU Engine(*)\Utilization Percentage', '\GPU Process Memory(*)\Dedicated Usage') -ErrorAction SilentlyContinue).CounterSamples
    $agg = @{}
    foreach ($s in $samples) {
        if ($s.InstanceName -notmatch '^pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+(?:_eng_(\d+)_engtype_.*)?$') { continue }
        $procId = [int]$Matches[1]; $luid = $Matches[2]; $eng = $Matches[3]
        if (-not $luidToBus.ContainsKey($luid)) { continue }
        $key = "$procId|$luid"
        if (-not $agg.ContainsKey($key)) { $agg[$key] = @{ pid = $procId; bus = $luidToBus[$luid]; mem = 0.0; eng = @{} } }
        if ($s.Path -like '*dedicated usage') { $agg[$key].mem += $s.CookedValue }
        elseif ($eng) { $agg[$key].eng[$eng] = [double]($agg[$key].eng[$eng]) + $s.CookedValue }
    }
    $names = @{}
    foreach ($p in Get-Process) { $names[$p.Id] = $p.ProcessName }
    foreach ($a in $agg.Values) {
        $util = 0.0
        foreach ($v in $a.eng.Values) { if ($v -gt $util) { $util = $v } }
        $memMiB = [math]::Round($a.mem / 1MB)
        if ($memMiB -le 0 -and $util -le 0.05) { continue }
        $name = $names[$a.pid]; if (-not $name) { $name = "pid $($a.pid)" }
        $out.procs += [ordered]@{ pid = $a.pid; name = $name; bus = $a.bus
                                  util_pct = [math]::Round([math]::Min(100, $util), 1); mem_mib = $memMiB }
    }
    if (-not $samples) { $out.errors += 'no GPU performance counters (English counter names are required)' }
} catch {
    $out.errors += "$_"
}
$out | ConvertTo-Json -Depth 5 -Compress
