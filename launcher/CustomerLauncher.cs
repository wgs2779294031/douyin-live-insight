// Stable .NET Framework 4.x entry. No product DLL is loaded before admission.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

namespace CustomerLocal {
sealed class Failure : Exception {
    public readonly string Code; public readonly int Exit;
    public Failure(string code, int exit) : base(code) { Code=code; Exit=exit; }
}
static class Check {
    public static void That(bool value, string code) { if(!value) throw new Failure(code,78); }
    public static Dictionary<string,object> Map(object value) { var v=value as Dictionary<string,object>; That(v!=null,"invalid_facts");return v; }
    public static List<object> Array(object value) { var v=value as List<object>;That(v!=null,"invalid_facts");return v; }
    public static string Text(object value) { var v=value as string;That(v!=null,"invalid_facts");return v; }
    public static object Get(Dictionary<string,object> map,string key) { That(map.ContainsKey(key),"invalid_facts");return map[key]; }
    public static string S(Dictionary<string,object> map,string key) { return Text(Get(map,key)); }
    public static long Number(object value) { That(value is long,"invalid_facts");return (long)value; }
    public static bool Flag(object value) { That(value is bool,"invalid_facts");return (bool)value; }
    public static void Keys(Dictionary<string,object> map,params string[] keys) {
        That(map.Count==keys.Length,"invalid_facts");foreach(string key in keys) That(map.ContainsKey(key),"invalid_facts");
    }
    public static void Schema(Dictionary<string,object> map,string kind) { That(Number(Get(map,"schema"))==1 && S(map,"kind")==kind,"invalid_facts"); }
    public static bool Hex(string value,int count) { return value!=null && Regex.IsMatch(value,"\\A[0-9a-f]{"+count.ToString(CultureInfo.InvariantCulture)+"}\\z"); }
    public static bool Uuid(string value) { Guid id;return Guid.TryParseExact(value,"D",out id) && id.ToString("D")==value; }
}

// Bounded strict JSON, including duplicate-key rejection. No polymorphic types.
sealed class Json {
    readonly string text;int at;int nodes;
    Json(string value) { text=value; }
    public static object Parse(byte[] raw) {
        var parser=new Json(new UTF8Encoding(false,true).GetString(raw));var value=parser.Value(0);parser.White();
        Check.That(parser.at==parser.text.Length,"invalid_json");return value;
    }
    void White() { while(at<text.Length && (text[at]==' '||text[at]=='\t'||text[at]=='\r'||text[at]=='\n'))at++; }
    object Value(int depth) {
        Check.That(depth<=32 && ++nodes<=200000,"json_limit");White();Check.That(at<text.Length,"invalid_json");char c=text[at];
        if(c=='"')return String();
        if(c=='{') { at++;var map=new Dictionary<string,object>(StringComparer.Ordinal);White();if(Take('}'))return map;
            while(true) { White();Check.That(at<text.Length && text[at]=='"',"invalid_json");string key=String();White();Check.That(Take(':'),"invalid_json");
                Check.That(!map.ContainsKey(key),"duplicate_json_key");map.Add(key,Value(depth+1));White();if(Take('}'))return map;Check.That(Take(','),"invalid_json"); }
        }
        if(c=='[') { at++;var list=new List<object>();White();if(Take(']'))return list;
            while(true) { list.Add(Value(depth+1));White();if(Take(']'))return list;Check.That(Take(','),"invalid_json"); }
        }
        if(Literal("true"))return true;if(Literal("false"))return false;if(Literal("null"))return null;
        int start=at;if(c=='-')at++;Check.That(at<text.Length,"invalid_json");
        if(text[at]=='0')at++;else { Check.That(text[at]>='1'&&text[at]<='9',"invalid_json");while(at<text.Length && Char.IsDigit(text[at]))at++; }
        bool fractional=false;
        if(at<text.Length && text[at]=='.') { fractional=true;at++;int p=at;while(at<text.Length && text[at]>='0'&&text[at]<='9')at++;Check.That(at>p,"invalid_json"); }
        if(at<text.Length && (text[at]=='e'||text[at]=='E')) { fractional=true;at++;if(at<text.Length&&(text[at]=='+'||text[at]=='-'))at++;int p=at;while(at<text.Length&&text[at]>='0'&&text[at]<='9')at++;Check.That(at>p,"invalid_json"); }
        string token=text.Substring(start,at-start);long integer;double number;
        if(!fractional && Int64.TryParse(token,NumberStyles.AllowLeadingSign,CultureInfo.InvariantCulture,out integer))return integer;
        Check.That(Double.TryParse(token,NumberStyles.Float,CultureInfo.InvariantCulture,out number)&&!Double.IsInfinity(number)&&!Double.IsNaN(number),"invalid_json_number");return number;
    }
    bool Take(char c) { if(at<text.Length&&text[at]==c){at++;return true;}return false; }
    bool Literal(string value) { if(at+value.Length<=text.Length && System.String.CompareOrdinal(text,at,value,0,value.Length)==0){at+=value.Length;return true;}return false; }
    string String() {
        Check.That(Take('"'),"invalid_json");var result=new StringBuilder();
        while(at<text.Length) { char c=text[at++];if(c=='"')return result.ToString();Check.That(c>=32,"invalid_json");
            if(c!='\\'){result.Append(c);continue;}Check.That(at<text.Length,"invalid_json");c=text[at++];
            switch(c) { case '"':case '\\':case '/':result.Append(c);break;case 'b':result.Append('\b');break;case 'f':result.Append('\f');break;
                case 'n':result.Append('\n');break;case 'r':result.Append('\r');break;case 't':result.Append('\t');break;
                case 'u':Check.That(at+4<=text.Length,"invalid_json");ushort value;Check.That(UInt16.TryParse(text.Substring(at,4),NumberStyles.HexNumber,CultureInfo.InvariantCulture,out value),"invalid_json");result.Append((char)value);at+=4;break;
                default:throw new Failure("invalid_json",78); }
        }throw new Failure("invalid_json",78);
    }
    public static byte[] Bytes(object value) { return new UTF8Encoding(false,true).GetBytes(Dump(value)+"\n"); }
    public static string Dump(object value) {
        if(value==null)return "null";if(value is bool)return (bool)value?"true":"false";
        var str=value as string;if(str!=null) { var b=new StringBuilder("\"");foreach(char c in str) {
            if(c=='"'||c=='\\'){b.Append('\\');b.Append(c);}else if(c<32){b.Append("\\u");b.Append(((int)c).ToString("x4",CultureInfo.InvariantCulture));}else b.Append(c);
        }return b.Append('"').ToString(); }
        var map=value as Dictionary<string,object>;if(map!=null) { var keys=new List<string>(map.Keys);keys.Sort(StringComparer.Ordinal);var parts=new List<string>();foreach(string key in keys)parts.Add(Dump(key)+":"+Dump(map[key]));return "{"+System.String.Join(",",parts.ToArray())+"}"; }
        var list=value as List<object>;if(list!=null) { var parts=new List<string>();foreach(object item in list)parts.Add(Dump(item));return "["+System.String.Join(",",parts.ToArray())+"]"; }
        if(value is long || value is int)return Convert.ToString(value,CultureInfo.InvariantCulture);
        throw new Failure("invalid_serialization",78);
    }
}

static class Native {
    public const uint READ=0x80000000, WRITE=0x40000000, SHARE_READ=1, SHARE_WRITE=2, SHARE_DELETE=4;
    public const uint OPEN_EXISTING=3, OPEN_REPARSE=0x200000, BACKUP=0x02000000, ATTR_NORMAL=0x80;
    public const uint EXTENDED_STARTUPINFO_PRESENT=0x00080000, CREATE_UNICODE_ENVIRONMENT=0x400, CREATE_NO_WINDOW=0x08000000, CREATE_SUSPENDED=4;
    public const int PROC_THREAD_ATTRIBUTE_HANDLE_LIST=0x20002, PROC_THREAD_ATTRIBUTE_JOB_LIST=0x2000d;
    const uint JobLimitActiveProcess=0x8, JobLimitKillOnClose=0x2000;
    const int JobObjectExtendedLimitInformation=9;
    public static readonly IntPtr Invalid=new IntPtr(-1);
    [StructLayout(LayoutKind.Sequential)] public struct Security { public int length;public IntPtr descriptor;[MarshalAs(UnmanagedType.Bool)]public bool inherit; }
    [StructLayout(LayoutKind.Sequential)] public struct FileInfo { public uint attributes;public uint creationLow,creationHigh,accessLow,accessHigh,writeLow,writeHigh,volume,sizeHigh,sizeLow,links,indexHigh,indexLow; }
    [StructLayout(LayoutKind.Sequential)] public struct FileId { public ulong volume;[MarshalAs(UnmanagedType.ByValArray,SizeConst=16)]public byte[] id; }
    [StructLayout(LayoutKind.Sequential)] public struct Times { public uint low,high; }
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] public struct Startup {
        public int cb;public string reserved,desktop,title;public uint x,y,xsize,ysize,xchars,ychars,fill,flags;public short show,reserved2;public IntPtr reservedPointer,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] public struct StartupEx { public Startup startup;public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)] public struct ProcessInfo { public IntPtr process,thread;public uint pid,tid; }
    [StructLayout(LayoutKind.Sequential)] struct JobBasicLimits {
        public long processTime,jobTime; public uint flags; public UIntPtr minimumWorkingSet,maximumWorkingSet;
        public uint activeProcessLimit; public UIntPtr affinity; public uint priority,scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct JobIoCounters {
        public ulong readOperations,writeOperations,otherOperations,readBytes,writeBytes,otherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct JobLimits {
        public JobBasicLimits basic; public JobIoCounters io;
        public UIntPtr processMemory,jobMemory,peakProcessMemory,peakJobMemory;
    }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern SafeFileHandle CreateFileW(string path,uint access,uint share,ref Security security,uint disposition,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool GetFileInformationByHandle(SafeFileHandle handle,out FileInfo info);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool GetFileInformationByHandleEx(SafeFileHandle handle,int kind,out FileId info,uint bytes);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool FlushFileBuffers(SafeFileHandle handle);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern bool MoveFileExW(string source,string target,uint flags);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool GetProcessTimes(IntPtr process,out Times created,out Times exited,out Times kernel,out Times user);
    [DllImport("kernel32.dll")] public static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] public static extern uint GetCurrentProcessId();
    [DllImport("kernel32.dll",SetLastError=true)] public static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool GetExitCodeProcess(IntPtr process,out uint code);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern IntPtr GetStdHandle(int kind);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool DuplicateHandle(IntPtr sourceProcess,IntPtr source,IntPtr targetProcess,out IntPtr target,uint access,bool inherit,uint options);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool WriteFile(IntPtr file,byte[] buffer,uint count,out uint written,IntPtr overlapped);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool ReadFile(IntPtr file,byte[] buffer,uint count,out uint read,IntPtr overlapped);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,uint flags,ref IntPtr size);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returned);
    [DllImport("kernel32.dll")] public static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern bool CreateProcessW(string application,StringBuilder command,IntPtr processSecurity,IntPtr threadSecurity,bool inherit,uint flags,IntPtr environment,string cwd,ref StartupEx startup,out ProcessInfo process);
    [DllImport("kernel32.dll",ExactSpelling=true,SetLastError=true)] static extern SafeFileHandle CreateJobObjectW(IntPtr security,IntPtr name);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(SafeFileHandle job,int kind,ref JobLimits limits,uint bytes);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool IsProcessInJob(IntPtr process,SafeFileHandle job,out bool member);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern uint ResumeThread(IntPtr thread);

    public static SafeFileHandle NewMcpJob() {
        int size=Marshal.SizeOf(typeof(JobLimits));
        Check.That(size==144,"mcp_job_abi_unavailable"); // Packaged Windows x64.
        var job=CreateJobObjectW(IntPtr.Zero,IntPtr.Zero); // Unnamed and non-inheritable.
        try {
            Check.That(!job.IsInvalid,"mcp_job_unavailable");
            var limits=new JobLimits { basic=new JobBasicLimits { flags=JobLimitActiveProcess|JobLimitKillOnClose, activeProcessLimit=1 } };
            Check.That(SetInformationJobObject(job,JobObjectExtendedLimitInformation,ref limits,(uint)size),"mcp_job_unavailable");
            return job;
        }catch {job.Dispose();throw;}
    }

    public static SafeFileHandle Open(string path,uint access,uint share,bool inherit,bool directory) {
        var security=new Security { length=Marshal.SizeOf(typeof(Security)),inherit=inherit };
        var handle=CreateFileW(path,access,share,ref security,OPEN_EXISTING,OPEN_REPARSE|(directory?BACKUP:ATTR_NORMAL),IntPtr.Zero);
        if(handle.IsInvalid) { int error=Marshal.GetLastWin32Error();handle.Dispose();throw new Failure(error==32||error==33?"gate_busy":"path_unavailable",error==32||error==33?75:78); }
        FileInfo info;if(!GetFileInformationByHandle(handle,out info)){handle.Dispose();throw new Failure("path_unavailable",78);}
        if((info.attributes&0x400)!=0 || ((info.attributes&0x10)!=0)!=directory || (!directory&&info.links!=1)) { handle.Dispose();throw new Failure("linked_or_special_path",78); }
        return handle;
    }
    public static Dictionary<string,object> Identity(IntPtr process,uint pid) {
        Times created,exited,kernel,user;Check.That(GetProcessTimes(process,out created,out exited,out kernel,out user),"identity_unknown");
        ulong time=((ulong)created.high<<32)|created.low;
        return Program.M("pid",(long)pid,"created_filetime",time.ToString(CultureInfo.InvariantCulture));
    }
    public static bool Alive(Dictionary<string,object> identity) {
        uint pid=(uint)Check.Number(Check.Get(identity,"pid"));var process=OpenProcess(0x1000,false,pid);if(process==IntPtr.Zero)return false;
        try { uint code;return GetExitCodeProcess(process,out code)&&code==259&&Json.Dump(Identity(process,pid))==Json.Dump(identity); }
        catch(Failure){return false;}finally{CloseHandle(process);}
    }
    public static string State(Dictionary<string,object> identity) {
        uint pid=(uint)Check.Number(Check.Get(identity,"pid"));var process=OpenProcess(0x1000,false,pid);
        if(process==IntPtr.Zero) { int error=Marshal.GetLastWin32Error();return error==87||error==1168?"ended":"unknown"; }
        try { uint code;if(!GetExitCodeProcess(process,out code))return "unknown";
            if(Json.Dump(Identity(process,pid))!=Json.Dump(identity))return "ended";
            return code==259?"alive":"ended";
        }catch(Failure){return "unknown";}finally{CloseHandle(process);}
    }
    public static void Write(IntPtr handle,byte[] bytes) {
        if(handle==IntPtr.Zero||handle==Invalid)return;int at=0;
        while(at<bytes.Length) { byte[] part=new byte[bytes.Length-at];Buffer.BlockCopy(bytes,at,part,0,part.Length);uint written;
            if(!WriteFile(handle,part,(uint)part.Length,out written,IntPtr.Zero)||written==0)return;at+=(int)written; }
    }
}

sealed class Pins : IDisposable {
    readonly List<SafeFileHandle> handles=new List<SafeFileHandle>();
    readonly HashSet<string> directories=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    public void Directory(string path) {
        path=Paths.Absolute(path);var stack=new Stack<string>();for(string current=path;current!=null;current=Path.GetDirectoryName(current))stack.Push(current);
        while(stack.Count>0) { string current=stack.Pop();if(directories.Add(current))handles.Add(Native.Open(current,0x81,Native.SHARE_READ|Native.SHARE_WRITE,false,true)); }
    }
    public SafeFileHandle File(string path,uint share) { Directory(Path.GetDirectoryName(path));var handle=Native.Open(path,Native.READ,share,false,false);handles.Add(handle);return handle; }
    public byte[] Read(string path,int cap,bool immutable) {
        if(immutable)return Read(File(path,Native.SHARE_READ),cap);
        Directory(Path.GetDirectoryName(path));
        using(var handle=Native.Open(path,Native.READ,Native.SHARE_READ|Native.SHARE_WRITE|Native.SHARE_DELETE,false,false))return Read(handle,cap);
    }
    static byte[] Read(SafeFileHandle handle,int cap) {
        Native.FileInfo before,after;Check.That(Native.GetFileInformationByHandle(handle,out before),"path_unavailable");
        ulong size=((ulong)before.sizeHigh<<32)|before.sizeLow;Check.That(size<=(ulong)cap,"record_too_large");
        byte[] raw;
        using(var borrowed=new SafeFileHandle(handle.DangerousGetHandle(),false))
        using(var stream=new FileStream(borrowed,FileAccess.Read,4096,false)) { using(var memory=new MemoryStream()) { byte[] buffer=new byte[8192];int count;
            while((count=stream.Read(buffer,0,buffer.Length))>0) { Check.That(memory.Length+count<=cap,"record_too_large");memory.Write(buffer,0,count); }raw=memory.ToArray(); }
            Check.That(Native.GetFileInformationByHandle(handle,out after)&&before.sizeHigh==after.sizeHigh&&before.sizeLow==after.sizeLow&&before.writeHigh==after.writeHigh&&before.writeLow==after.writeLow,"record_changed");
        }
        return raw;
    }
    public Dictionary<string,object> DirectoryIdentity(string path) {
        Directory(path);using(var handle=Native.Open(path,0x81,Native.SHARE_READ|Native.SHARE_WRITE,false,true)) { Native.FileId info;
            Check.That(Native.GetFileInformationByHandleEx(handle,18,out info,24),"identity_unknown");var hex=new StringBuilder();foreach(byte value in info.id)hex.Append(value.ToString("x2",CultureInfo.InvariantCulture));
            return Program.M("volume_serial",info.volume.ToString("x16",CultureInfo.InvariantCulture),"file_id",hex.ToString()); }
    }
    public void Dispose() { for(int i=handles.Count-1;i>=0;i--)handles[i].Dispose();handles.Clear(); }
}

sealed class Installation : IDisposable {
    public const string LegacyVersion="0.1.0-local.11B1", TransactionVersion="0.1.0-local.11B2";
    public readonly Pins pins=new Pins();
    public readonly string target,id,manifestSha,managerManifestSha,cache;
    readonly string ownerSha,descriptorSha,admissionGeneration;
    readonly bool migratedCache;
    public readonly Dictionary<string,object> owner,manifest,layout,identity;
    public readonly Dictionary<string,Dictionary<string,object>> files=new Dictionary<string,Dictionary<string,object>>(StringComparer.Ordinal);
    public readonly Dictionary<string,string> context=new Dictionary<string,string>(StringComparer.Ordinal);
    public Installation(string path,bool product) {
        target=Paths.Absolute(path);pins.Directory(target);
        // Read the epoch before sampling version facts. A later matching
        // manifest cannot make a pre-commit request current again.
        var marker=product?Gate.ReadMaintenance(pins,target):null;
        byte[] ownerRaw=pins.Read(Path.Combine(target,"owner.json"),65536,false);
        ownerSha=Program.Hash(ownerRaw);owner=Check.Map(Json.Parse(ownerRaw));
        Check.Keys(owner,"schema","kind","install_id","target","target_identity","version","channel","bundle_manifest_sha256","manager_cache","manager_manifest_sha256");
        Check.Schema(owner,"customer_installation_owner");id=Check.S(owner,"install_id");Check.That(Check.Uuid(id),"invalid_owner");
        Check.That(Paths.Same(Check.S(owner,"target"),target),"owner_target_mismatch");identity=pins.DirectoryIdentity(target);
        ValidateIdentity(Check.Map(Check.Get(owner,"target_identity")));
        Check.That(Json.Dump(identity)==Json.Dump(Check.Get(owner,"target_identity")),"owner_identity_mismatch");
        if(marker!=null) {
            Check.That(Check.S(marker,"install_id")==id,"gate_owner_mismatch");
            if(Check.Flag(Check.Get(marker,"active")))throw new Failure("maintenance_active",75);
            admissionGeneration=Gate.Generation(marker);
        }
        manifestSha=Check.S(owner,"bundle_manifest_sha256");managerManifestSha=Check.S(owner,"manager_manifest_sha256");
        Check.That(Check.Hex(manifestSha,64)&&Check.Hex(managerManifestSha,64),"invalid_owner");
        byte[] raw=pins.Read(Path.Combine(target,"installed.manifest.json"),8*1024*1024,false);Check.That(Program.Hash(raw)==manifestSha,"manifest_mismatch");
        manifest=Check.Map(Json.Parse(raw));ValidateManifest(manifest,files);
        Check.That(Check.S(manifest,"version")==Check.S(owner,"version")&&Check.S(manifest,"channel")==Check.S(owner,"channel"),"owner_version_mismatch");
        cache=Paths.Absolute(Check.S(owner,"manager_cache"));
        string expected=Path.Combine(Path.GetDirectoryName(target),"."+Path.GetFileName(target)+".manager-"+id);
        migratedCache=Paths.Same(cache,expected+"-b2");
        Check.That((migratedCache||(Paths.Same(cache,expected)&&managerManifestSha==manifestSha))&&
            !Paths.Within(cache,target)&&!Paths.Within(target,cache),"manager_cache_mismatch");
        byte[] descriptorRaw=pins.Read(Path.Combine(target,"installation.json"),65536,false);
        descriptorSha=Program.Hash(descriptorRaw);layout=Check.Map(Json.Parse(descriptorRaw));
        Check.Keys(layout,"schema","kind","app","state","python","node","ffmpeg","model","cache","browser","dll_directories","ports");
        Check.Schema(layout,"customer_installation");
        foreach(var pair in new Dictionary<string,string>{{"app","app"},{"state","state"},{"python","runtime/python/python.exe"},{"node","runtime/node/node.exe"},{"ffmpeg","runtime/ffmpeg/ffmpeg.exe"},{"model","assets/models/faster-whisper-small"},{"cache","state/cache"}})
            Check.That(Check.S(layout,pair.Key)==pair.Value,"installation_layout_mismatch");
        var ports=Check.Map(Check.Get(layout,"ports"));Check.Keys(ports,"board","cdp");long board=Check.Number(Check.Get(ports,"board")),cdp=Check.Number(Check.Get(ports,"cdp"));
        Check.That(board>0&&board<=65535&&cdp>0&&cdp<=65535&&board!=cdp,"invalid_ports");
        var browser=Check.Map(Check.Get(layout,"browser"));string browserKind=Check.S(browser,"kind");
        if(browserKind=="system-edge")Check.Keys(browser,"kind");else {Check.Keys(browser,"kind","path");Check.That(browserKind=="explicit","invalid_browser");Paths.Absolute(Check.S(browser,"path"));}
        var dlls=Check.Array(Check.Get(layout,"dll_directories"));Check.That(dlls.Count<=64,"invalid_layout");var seen=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(object item in dlls) { string name=Paths.Relative(Check.Text(item));Check.That(name=="runtime/python"||name.StartsWith("runtime/python/",StringComparison.Ordinal),"invalid_layout");Check.That(seen.Add(name),"invalid_layout"); }
        context.Add("root",Path.Combine(target,"app"));context.Add("config",Path.Combine(target,"state/config.json"));context.Add("data",Path.Combine(target,"state/data"));
        context.Add("roster",Path.Combine(target,"state/anchors.json"));context.Add("dist",Path.Combine(target,"app/web/dist"));context.Add("python",Path.Combine(target,"runtime/python/python.exe"));
        context.Add("port",board.ToString(CultureInfo.InvariantCulture));context.Add("cdp-port",cdp.ToString(CultureInfo.InvariantCulture));
    }
    public void VerifyCurrent(Gate gate) {
        if(gate.Generation()!=admissionGeneration)throw new Failure("installation_snapshot_changed",75);
        using(var fresh=new Pins()) {
            if(Program.Hash(fresh.Read(Path.Combine(target,"owner.json"),65536,false))!=ownerSha||
               Program.Hash(fresh.Read(Path.Combine(target,"installed.manifest.json"),8*1024*1024,false))!=manifestSha||
               Program.Hash(fresh.Read(Path.Combine(target,"installation.json"),65536,false))!=descriptorSha)
                throw new Failure("installation_snapshot_changed",75);
        }
    }
    public void VerifyManagerVersion(Dictionary<string,object> value) {
        if(migratedCache)Check.That(Check.S(value,"version")==TransactionVersion,"manager_cache_version_mismatch");
    }
    public static void ValidateIdentity(Dictionary<string,object> value) {
        Check.Keys(value,"volume_serial","file_id");Check.That(Check.Hex(Check.S(value,"volume_serial"),16)&&Check.Hex(Check.S(value,"file_id"),32),"invalid_identity");
    }
    public static void ValidateManifest(Dictionary<string,object> value,Dictionary<string,Dictionary<string,object>> rows) {
        Check.Schema(value,"customer_bundle");string version=Check.S(value,"version");
        Check.That((version==LegacyVersion||version==TransactionVersion)&&Check.S(value,"channel")=="local-candidate"&&
            Check.Number(Check.Get(value,"state_compatibility"))==1,"unsupported_bundle");
        var files=Check.Array(Check.Get(value,"files"));var dirs=Check.Array(Check.Get(value,"directories"));Check.That(files.Count<=20000&&dirs.Count<=5000,"manifest_limit");
        var folded=new HashSet<string>(StringComparer.OrdinalIgnoreCase);var directories=new HashSet<string>(StringComparer.OrdinalIgnoreCase);long total=0;
        foreach(object item in files) { var row=Check.Map(item);Check.Keys(row,"path","bytes","sha256","component");string name=Paths.Relative(Check.S(row,"path"));long count=Check.Number(Check.Get(row,"bytes"));string kind=Check.S(row,"component");
            Check.That(folded.Add(name)&&count>=0&&count<=600L*1024*1024&&Check.Hex(Check.S(row,"sha256"),64),"invalid_manifest");total=checked(total+count);Check.That(total<=4L*1024*1024*1024,"manifest_limit");
            Check.That((kind=="product"&&name.StartsWith("payload/",StringComparison.Ordinal))||(kind=="manager"&&name.StartsWith("manager/",StringComparison.Ordinal))||
                (kind=="entry"&&name.StartsWith("entry/",StringComparison.Ordinal))||(kind=="bootstrap"&&name=="CustomerSetup.exe")||(kind=="guide"&&name=="AGENT_INSTALL.md"),"invalid_manifest_component");
            rows.Add(name,row);
        }
        foreach(object item in dirs) { string name=Paths.Relative(Check.Text(item));Check.That(directories.Add(name)&&!folded.Contains(name),"manifest_alias"); }
        foreach(string name in folded) { string parent=name;while(parent.Contains("/")) { parent=parent.Substring(0,parent.LastIndexOf('/'));Check.That(directories.Contains(parent)&&!folded.Contains(parent),"manifest_directory_missing"); } }
        foreach(string name in directories) { string parent=name;while(parent.Contains("/")) { parent=parent.Substring(0,parent.LastIndexOf('/'));Check.That(directories.Contains(parent)&&!folded.Contains(parent),"manifest_directory_missing"); } }
    }
    public void VerifyProduct(string relative) {
        string key="payload/"+relative;Check.That(files.ContainsKey(key)&&Check.S(files[key],"component")=="product","unlisted_product_entry");
        VerifyFile(pins,target,relative,files[key]);
    }
    public static void VerifyFile(Pins pins,string root,string relative,Dictionary<string,object> row) {
        string path=Paths.Join(root,relative);var handle=pins.File(path,Native.SHARE_READ);Native.FileInfo info;Check.That(Native.GetFileInformationByHandle(handle,out info),"path_unavailable");
        long size=((long)info.sizeHigh<<32)|info.sizeLow;Check.That(size==Check.Number(Check.Get(row,"bytes")),"file_integrity_mismatch");
        using(var borrowed=new SafeFileHandle(handle.DangerousGetHandle(),false))using(var stream=new FileStream(borrowed,FileAccess.Read,65536,false))using(var hash=SHA256.Create()) {
            var text=new StringBuilder();foreach(byte value in hash.ComputeHash(stream))text.Append(value.ToString("x2",CultureInfo.InvariantCulture));
            Check.That(text.ToString()==Check.S(row,"sha256"),"file_integrity_mismatch");
        }
    }
    public void Dispose() { pins.Dispose(); }
}

sealed class Gate : IDisposable {
    const string McpIsolation="single_process_job_v1";
    readonly Installation install;readonly string folder,admissionPath,leasePath,maintenancePath,ledgerPath;
    SafeFileHandle admission;
    public Dictionary<string,object> maintenance,ledger;
    public Gate(Installation value) { install=value;folder=Path.Combine(value.target,".gate");value.pins.Directory(folder);
        admissionPath=Path.Combine(folder,"admission.lock");leasePath=Path.Combine(folder,"code.lease");maintenancePath=Path.Combine(folder,"maintenance.json");ledgerPath=Path.Combine(folder,"spawn-ledger.json"); }
    public void Enter(int waitMilliseconds=500) {
        var watch=Stopwatch.StartNew();while(true) { try { admission=Native.Open(admissionPath,Native.READ|Native.WRITE,0,false,false);break; }
            catch(Failure error) { if(error.Exit!=75||watch.ElapsedMilliseconds>=waitMilliseconds)throw;System.Threading.Thread.Sleep(10); } }
        try { Read(); }catch { Leave();throw; }
    }
    public void Leave() { if(admission!=null){admission.Dispose();admission=null;} }
    public static Dictionary<string,object> ReadMaintenance(Pins pins,string target) {
        var value=Check.Map(Json.Parse(pins.Read(Path.Combine(target,".gate","maintenance.json"),65536,false)));
        long schema=Check.Number(Check.Get(value,"schema"));
        if(schema==1)Check.Keys(value,"schema","kind","install_id","active","token");
        else {
            Check.That(schema==2,"invalid_maintenance");
            Check.Keys(value,"schema","kind","install_id","active","token","generation");
            Check.That(Check.Uuid(Check.S(value,"generation")),"invalid_maintenance");
        }
        Check.That(Check.S(value,"kind")=="customer_maintenance"&&Check.Uuid(Check.S(value,"install_id")),"invalid_maintenance");
        bool active=Check.Flag(Check.Get(value,"active"));string token=Check.S(value,"token");
        Check.That(active?Check.Uuid(token):token=="","invalid_maintenance");return value;
    }
    public static string Generation(Dictionary<string,object> value) {
        return value.ContainsKey("generation")?Check.S(value,"generation"):"";
    }
    public string Generation() { Check.That(admission!=null,"admission_required");return Generation(maintenance); }
    void Read() {
        using(var pins=new Pins()) {maintenance=ReadMaintenance(pins,install.target);ledger=Check.Map(Json.Parse(pins.Read(ledgerPath,65536,false))); }
        Check.That(Check.S(maintenance,"install_id")==install.id,"gate_owner_mismatch");
        Check.Keys(ledger,"schema","kind","install_id","active","recent");Check.Schema(ledger,"customer_spawn_ledger");Check.That(Check.S(ledger,"install_id")==install.id,"gate_owner_mismatch");
        var ids=new HashSet<string>(StringComparer.Ordinal);
        foreach(string key in new[]{"active","recent"}) { var entries=Check.Array(Check.Get(ledger,key));Check.That(entries.Count<=32,"ledger_limit");
            foreach(object item in entries) { var row=Check.Map(item);
                if(row.ContainsKey("isolation"))Check.Keys(row,"id","mode","phase","launcher","child","isolation");
                else Check.Keys(row,"id","mode","phase","launcher","child");
                Check.That(Check.Hex(Check.S(row,"id"),32)&&ids.Add(Check.S(row,"id")),"invalid_intent");string mode=Check.S(row,"mode"),phase=Check.S(row,"phase");
                Check.That(Array.IndexOf(new[]{"start","stop","status","run","mcp","doctor-imports"},mode)>=0,"invalid_intent");
                Check.That(Array.IndexOf(key=="active"?new[]{"reserved","creating","running","unknown"}:new[]{"child_exited","not_created"},phase)>=0,"invalid_intent");
                Identity(Check.Map(Check.Get(row,"launcher")));if(Check.Get(row,"child")!=null)Identity(Check.Map(Check.Get(row,"child")));
                Check.That((phase!="running"&&phase!="child_exited")||Check.Get(row,"child")!=null,"invalid_intent");
                if(row.ContainsKey("isolation"))Check.That(mode=="mcp"&&phase=="running"
                    &&Check.S(row,"isolation")==McpIsolation,"invalid_intent");
            }
        }
    }
    public static void Identity(Dictionary<string,object> value) {
        Check.Keys(value,"pid","created_filetime");long pid=Check.Number(Check.Get(value,"pid"));string created=Check.S(value,"created_filetime");ulong time;
        Check.That(pid>0&&pid<=UInt32.MaxValue&&Regex.IsMatch(created,"\\A[1-9][0-9]{0,19}\\z")&&UInt64.TryParse(created,out time),"invalid_process_identity");
    }
    public void Save(string path,Dictionary<string,object> value) {
        Check.That(admission!=null,"admission_required");byte[] raw=Json.Bytes(value);Check.That(raw.Length<=65536,"ledger_limit");
        string temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        using(var stream=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None,4096,FileOptions.WriteThrough)) {stream.Write(raw,0,raw.Length);stream.Flush(true);Check.That(Native.FlushFileBuffers(stream.SafeFileHandle),"gate_write_unknown");}
        if(!Native.MoveFileExW(temporary,path,1|8))throw new Failure("gate_write_unknown",78);
        using(var pins=new Pins())Check.That(Program.Hash(pins.Read(path,65536,false))==Program.Hash(raw),"gate_write_unknown");
    }
    public void SaveLedger() { Save(ledgerPath,ledger); }
    bool Ended(Dictionary<string,object> row) {
        if(Native.State(Check.Map(Check.Get(row,"launcher")))!="ended")return false;
        return Check.Get(row,"child")==null||Native.State(Check.Map(Check.Get(row,"child")))=="ended";
    }
    bool ReapEndedMcp() {
        Check.That(admission!=null,"admission_required");
        var active=Check.Array(Check.Get(ledger,"active"));bool removed=false;
        for(int i=active.Count-1;i>=0;i--) {
            var row=Check.Map(active[i]);
            // The proof is published only after the OS assigns the suspended
            // read-only MCP child to its private one-process Job. Legacy rows
            // and incomplete launches retain the normal fail-closed policy.
            if(Check.S(row,"mode")=="mcp"&&Check.S(row,"phase")=="running"&&row.ContainsKey("isolation")
                &&Check.S(row,"isolation")==McpIsolation&&Ended(row)) {
                active.RemoveAt(i);removed=true;
            }
        }
        return removed;
    }
    public void Prune() {
        ReapEndedMcp();
        var recent=Check.Array(Check.Get(ledger,"recent"));for(int i=recent.Count-1;i>=0;i--)if(Ended(Check.Map(recent[i])))recent.RemoveAt(i);
    }
    public int Unknown() { int count=0;foreach(object item in Check.Array(Check.Get(ledger,"active"))) { var row=Check.Map(item);if(Check.S(row,"phase")!="running"||Native.State(Check.Map(Check.Get(row,"launcher")))!="alive"||Native.State(Check.Map(Check.Get(row,"child")))!="alive")count++; }
        foreach(object item in Check.Array(Check.Get(ledger,"recent"))) { var row=Check.Map(item);if(Native.State(Check.Map(Check.Get(row,"launcher")))=="unknown"||(Check.Get(row,"child")!=null&&Native.State(Check.Map(Check.Get(row,"child")))=="unknown"))count++; }return count;
    }
    public int Settling() { int count=0;foreach(object item in Check.Array(Check.Get(ledger,"recent"))) { var row=Check.Map(item);
        if(Native.State(Check.Map(Check.Get(row,"launcher")))=="alive"||(Check.Get(row,"child")!=null&&Native.State(Check.Map(Check.Get(row,"child")))=="alive"))count++; }return count;
    }
    public Dictionary<string,object> Status() {
        if(ReapEndedMcp())SaveLedger();
        string lease="available";try { using(var handle=Native.Open(leasePath,Native.READ|Native.WRITE,0,false,false)){} }
        catch(Failure error){lease=error.Exit==75?"busy":"unknown";}
        int count=Check.Array(Check.Get(ledger,"active")).Count,unknown=Unknown(),settling=Settling();bool maintaining=Check.Flag(Check.Get(maintenance,"active"));
        string state=unknown>0||lease=="unknown"?"unknown":maintaining?"maintenance":count>0||settling>0||lease=="busy"?"busy":"ready";
        return Program.M("ok",state!="unknown","state",state,"install_id",install.id,"maintenance_active",maintaining,"code_lease",lease,
            "active_count",count,"recent_count",Check.Array(Check.Get(ledger,"recent")).Count,"unknown_count",unknown,"settling_count",settling);
    }
    public Dictionary<string,object> Reserve(string mode) {
        Check.That(admission!=null,"admission_required");if(Check.Flag(Check.Get(maintenance,"active")))throw new Failure("maintenance_active",75);
        Prune();if(Unknown()>0)throw new Failure("holders_unconfirmed",75);var active=Check.Array(Check.Get(ledger,"active"));var recent=Check.Array(Check.Get(ledger,"recent"));
        // A second official run must not map Python merely to fail the inner
        // run lock with3, which would conservatively poison its new intent.
        // Keep an existing run reserved until its own clean settlement, even
        // if it exits immediately after the identity check above.
        if(mode=="run")foreach(object item in active)if(Check.S(Check.Map(item),"mode")=="run")throw new Failure("application_run_busy",75);
        if(active.Count+recent.Count>=32)throw new Failure("ledger_busy",75);
        var row=Program.M("id",Guid.NewGuid().ToString("N"),"mode",mode,"phase","reserved","launcher",Native.Identity(Native.GetCurrentProcess(),Native.GetCurrentProcessId()),"child",null);
        active.Add(row);SaveLedger();return row;
    }
    public SafeFileHandle ReadLease() { return Native.Open(leasePath,Native.READ,Native.SHARE_READ,true,false); }
    public void Update(Dictionary<string,object> expected,string phase,Dictionary<string,object> child,bool terminal,bool isolatedMcp=false) {
        Check.That(admission!=null,"admission_required");var active=Check.Array(Check.Get(ledger,"active"));Dictionary<string,object> row=null;
        foreach(object item in active) { var value=Check.Map(item);if(Check.S(value,"id")==Check.S(expected,"id"))row=value; }
        Check.That(row!=null&&Check.S(row,"mode")==Check.S(expected,"mode")&&Json.Dump(Check.Get(row,"launcher"))==Json.Dump(Check.Get(expected,"launcher")),"intent_changed");
        object existing=Check.Get(row,"child");Check.That(existing==null||Json.Dump(existing)==Json.Dump(child),"intent_changed");
        if(isolatedMcp) {
            Check.That(Check.S(row,"mode")=="mcp"&&Check.S(row,"phase")=="creating"&&phase=="running"
                &&child!=null&&!terminal&&!row.ContainsKey("isolation"),"invalid_mcp_barrier");
            row.Add("isolation",McpIsolation);
        }
        row["phase"]=phase;row["child"]=child;
        if(terminal) {
            // Older launchers can read these terminal facts after rollback.
            row.Remove("isolation");active.Remove(row);Prune();
            var recent=Check.Array(Check.Get(ledger,"recent"));Check.That(recent.Count<32,"ledger_limit");recent.Add(row);
        }
        SaveLedger();
    }
    public SafeFileHandle Maintenance(string token) {
        Check.That(admission!=null&&Check.Uuid(token),"invalid_maintenance_token");
        if(Generation()!="")throw new Failure("use_transaction_manager",75);
        if(Check.Flag(Check.Get(maintenance,"active"))&&Check.S(maintenance,"token")!=token)throw new Failure("maintenance_owned_elsewhere",75);
        maintenance["active"]=true;maintenance["token"]=token;Save(maintenancePath,maintenance);
        Prune();if(Check.Array(Check.Get(ledger,"active")).Count!=0||Check.Array(Check.Get(ledger,"recent")).Count!=0)throw new Failure("holders_unconfirmed",75);
        SaveLedger();return Native.Open(leasePath,Native.READ|Native.WRITE,0,false,false);
    }
    public void Clear(string token,SafeFileHandle exclusive) {
        Check.That(admission!=null&&exclusive!=null&&!exclusive.IsClosed&&!exclusive.IsInvalid,"maintenance_lease_required");
        if(Generation()!="")throw new Failure("use_transaction_manager",75);
        Check.That(Check.Flag(Check.Get(maintenance,"active"))&&Check.S(maintenance,"token")==token,"maintenance_changed");
        Prune();Check.That(Check.Array(Check.Get(ledger,"active")).Count==0&&Check.Array(Check.Get(ledger,"recent")).Count==0,"holders_unconfirmed");
        maintenance["active"]=false;maintenance["token"]="";Save(maintenancePath,maintenance);SaveLedger();
    }
    public void Dispose() { Leave(); }
}

static class Paths {
    static readonly Regex Reserved=new Regex("\\A(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\\$|CONOUT\\$)(\\..*)?\\z",RegexOptions.IgnoreCase|RegexOptions.CultureInvariant);
    static void Component(string part) { string stem=part.Split('.')[0].TrimEnd(' ');Check.That(part.Length>0&&part.Length<=255&&part!="."&&part!=".."&&!part.EndsWith(".")&&!part.EndsWith(" ")&&!Reserved.IsMatch(stem)&&!Regex.IsMatch(part,"[<>:\"\\\\|?*\\x00-\\x1f]"),"invalid_path"); }
    public static string Relative(string value) { Check.That(value!=null&&value.Length<=1024,"invalid_path");string[] parts=value.Split('/');Check.That(parts.Length<=32,"invalid_path");foreach(string part in parts)Component(part);return value; }
    public static string Absolute(string value) {
        Check.That(value!=null&&value.Length>=3&&value.Length<240&&Regex.IsMatch(value,"\\A[A-Za-z]:[\\\\/]"),"invalid_path");
        Check.That(!value.StartsWith("\\\\")&&!value.StartsWith("//"),"invalid_path");string full=Path.GetFullPath(value).TrimEnd('\\','/');
        if(full.Length==2)full+="\\";string tail=full.Substring(3);if(tail.Length>0)foreach(string part in tail.Split('\\'))Component(part);
        // Validate original spelling as well: GetFullPath must not normalize ADS/trailing aliases away.
        string original=value.Substring(3).Replace('/','\\').TrimEnd('\\');if(original.Length>0)foreach(string part in original.Split('\\'))Component(part);
        return full;
    }
    public static bool Same(string left,string right) { return String.Equals(Absolute(left),Absolute(right),StringComparison.OrdinalIgnoreCase); }
    public static bool Within(string child,string root) { return Same(child,root)||Absolute(child).StartsWith(Absolute(root).TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase); }
    public static string Join(string root,string relative) { return Path.Combine(root,Relative(relative).Replace('/',Path.DirectorySeparatorChar)); }
}

static class Program {
    // Existing intents must publish their observed child identity or exit
    // even when another launcher is flushing its own admission record.
    const int IntentUpdateWaitMilliseconds=5000;
    // Kernel process teardown owns these handles; never dispose a successful
    // launch lease before the launcher and its original stdio handles end.
    static readonly List<SafeFileHandle> lifetimeLeases=new List<SafeFileHandle>();
    public static Dictionary<string,object> M(params object[] pairs) { var result=new Dictionary<string,object>(StringComparer.Ordinal);for(int i=0;i<pairs.Length;i+=2)result.Add((string)pairs[i],pairs[i+1]);return result; }
    public static string Hash(byte[] raw) { using(var hash=SHA256.Create()){var result=new StringBuilder();foreach(byte value in hash.ComputeHash(raw))result.Append(value.ToString("x2",CultureInfo.InvariantCulture));return result.ToString();} }
    static bool TransactionMode(string mode) {
        return mode=="upgrade"||mode=="rollback"||mode=="recover"||mode=="upgrade-status";
    }
    public static int Main(string[] arguments) {
        int code;
        try { code=Execute(arguments); }
        catch(Failure error) { Native.Write(Native.GetStdHandle(-12),Json.Bytes(M("ok",false,"code",error.Code)));code=error.Exit; }
        catch(Exception) { Native.Write(Native.GetStdHandle(-12),Json.Bytes(M("ok",false,"code","launcher_unavailable")));code=78; }
        GC.KeepAlive(lifetimeLeases);Environment.Exit(code);return code;
    }
    static Dictionary<string,string> Arguments(string[] arguments,out string mode) {
        Check.That(arguments.Length>0,"invalid_arguments");mode=arguments[0];
        Check.That(TransactionMode(mode)||Array.IndexOf(new[]{"install","start","stop","status","run","mcp","doctor","doctor-imports","gate-status","maintenance-acquire"},mode)>=0,"invalid_arguments");
        var values=new Dictionary<string,string>(StringComparer.Ordinal);
        for(int i=1;i<arguments.Length;i++) { string key=arguments[i];Check.That(key.StartsWith("--",StringComparison.Ordinal)&&key.Length>2,"invalid_arguments");key=key.Substring(2);
            Check.That(!values.ContainsKey(key),"duplicate_argument");
            if(key=="open"||key=="deep") { values.Add(key,"true");continue; }
            Check.That(i+1<arguments.Length,"invalid_arguments");values.Add(key,arguments[++i]);
        }
        var allowed=new HashSet<string>(new[]{"target"},StringComparer.Ordinal);
        if(mode=="install")foreach(string key in new[]{"manifest-sha256","port","cdp-port","browser","browser-path"})allowed.Add(key);
        else if(TransactionMode(mode))allowed.Add("manifest-sha256");
        else if(mode=="doctor") {allowed.Add("manifest-sha256");allowed.Add("deep");}
        else if(mode=="maintenance-acquire")allowed.Add("token");
        else if(mode!="gate-status") { foreach(string key in new[]{"root","config","data","roster","dist","python","port","cdp-port"})allowed.Add(key);if(mode=="start")allowed.Add("open"); }
        foreach(string key in values.Keys)Check.That(allowed.Contains(key),"unknown_argument");
        Check.That(values.ContainsKey("target"),"target_required");values["target"]=Paths.Absolute(values["target"]);
        if(mode=="install"||TransactionMode(mode))Check.That(values.ContainsKey("manifest-sha256"),"trusted_manifest_required");
        Check.That(!(values.ContainsKey("browser")&&values.ContainsKey("browser-path")),"duplicate_browser_argument");
        if(values.ContainsKey("manifest-sha256"))Check.That(Check.Hex(values["manifest-sha256"],64),"invalid_manifest_digest");
        if(mode=="maintenance-acquire")Check.That(values.ContainsKey("token")&&Check.Uuid(values["token"]),"invalid_maintenance_token");
        return values;
    }
    static int Execute(string[] arguments) {
        string mode;var values=Arguments(arguments,out mode);
        if(mode=="install"||TransactionMode(mode)||(mode=="doctor"&&values.ContainsKey("manifest-sha256")))return Setup(mode,values);
        bool product=mode!="doctor"&&mode!="gate-status"&&mode!="maintenance-acquire";
        using(var install=new Installation(values["target"],product)) {
            if(mode=="doctor")return Doctor(install,values);
            foreach(string key in install.context.Keys)if(values.ContainsKey(key)) {
                if(key=="port"||key=="cdp-port")Check.That(values[key]==install.context[key],"context_mismatch");
                else Check.That(Paths.Same(values[key],install.context[key]),"context_mismatch");
            }
            using(var gate=new Gate(install)) {
                if(mode=="gate-status") {gate.Enter();try{Native.Write(Native.GetStdHandle(-11),Json.Bytes(gate.Status()));return 0;}finally{gate.Leave();}}
                if(mode=="maintenance-acquire")return Maintenance(gate,values["token"]);
                return Product(install,gate,mode,values);
            }
        }
    }
    static string Quote(string value) {
        var b=new StringBuilder("\"");int slashes=0;
        foreach(char c in value) { if(c=='\\'){slashes++;continue;}if(c=='"'){b.Append('\\',slashes*2+1);b.Append('"');slashes=0;continue;}b.Append('\\',slashes);slashes=0;b.Append(c); }
        b.Append('\\',slashes*2);return b.Append('"').ToString();
    }
    static IntPtr Standard(int kind,uint access,bool preserveNull) {
        IntPtr original=Native.GetStdHandle(kind),result;
        if(original!=IntPtr.Zero&&original!=Native.Invalid) { Check.That(Native.DuplicateHandle(Native.GetCurrentProcess(),original,Native.GetCurrentProcess(),out result,0,true,2),"stdio_unavailable");return result; }
        if(preserveNull)return IntPtr.Zero;
        var security=new Native.Security{length=Marshal.SizeOf(typeof(Native.Security)),inherit=true};
        var handle=Native.CreateFileW("NUL",access,Native.SHARE_READ|Native.SHARE_WRITE,ref security,Native.OPEN_EXISTING,Native.ATTR_NORMAL,IntPtr.Zero);
        Check.That(!handle.IsInvalid,"stdio_unavailable");result=handle.DangerousGetHandle();handle.SetHandleAsInvalid();handle.Dispose();return result;
    }
    static IntPtr EnvironmentBlock(bool manager,string python,string contextPath) {
        var values=new SortedDictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        foreach(System.Collections.DictionaryEntry item in Environment.GetEnvironmentVariables()) { string key=(string)item.Key;if(!key.StartsWith("=",StringComparison.Ordinal))values[key]=(string)item.Value; }
        foreach(string key in new[]{"PYTHONHOME","PYTHONPATH","PYTHONUSERBASE","PYTHONSTARTUP","PYTHONINSPECT","NODE_OPTIONS","NODE_PATH","CDP_TARGET","CDP_TARGET_URL"})values.Remove(key);
        values["PYTHONDONTWRITEBYTECODE"]="1";
        string windows=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        values["PATH"]=Path.GetDirectoryName(python)+";"+Path.Combine(windows,"System32")+";"+windows;
        if(manager){values.Remove("DY_CUSTOMER_CONTEXT");values["PYTHONUTF8"]="1";}
        else values["DY_CUSTOMER_CONTEXT"]=contextPath;
        var b=new StringBuilder();foreach(var pair in values) {Check.That(!pair.Key.Contains("=")&&!pair.Key.Contains("\0")&&!pair.Value.Contains("\0"),"invalid_environment");b.Append(pair.Key).Append('=').Append(pair.Value).Append('\0');}b.Append('\0');
        return Marshal.StringToHGlobalUni(b.ToString());
    }
    static uint Spawn(string executable,List<string> args,string cwd,bool manager,string contextPath,SafeFileHandle lease,Action<Dictionary<string,object>> created,out bool called,out bool returnedFailure,bool isolatedMcp=false) {
        called=false;returnedFailure=false;var standard=new List<IntPtr>();
        IntPtr list=IntPtr.Zero,handles=IntPtr.Zero,environment=IntPtr.Zero,jobs=IntPtr.Zero;
        SafeFileHandle job=null;Native.ProcessInfo process=new Native.ProcessInfo();bool listInitialized=false;
        try {
            bool preserveNull=!manager&&String.Equals(Path.GetFileName(executable),"pythonw.exe",StringComparison.OrdinalIgnoreCase);
            standard.Add(Standard(-10,Native.READ,preserveNull));standard.Add(Standard(-11,Native.WRITE,preserveNull));standard.Add(Standard(-12,Native.WRITE,preserveNull));
            var inherited=new List<IntPtr>();foreach(IntPtr handle in standard)if(handle!=IntPtr.Zero&&handle!=Native.Invalid)inherited.Add(handle);if(lease!=null)inherited.Add(lease.DangerousGetHandle());
            handles=Marshal.AllocHGlobal(inherited.Count*IntPtr.Size);for(int i=0;i<inherited.Count;i++)Marshal.WriteIntPtr(handles,i*IntPtr.Size,inherited[i]);
            int attributeCount=isolatedMcp?2:1;
            IntPtr size=IntPtr.Zero;Native.InitializeProcThreadAttributeList(IntPtr.Zero,attributeCount,0,ref size);Check.That(size!=IntPtr.Zero,"handle_list_unavailable");list=Marshal.AllocHGlobal(size);
            listInitialized=Native.InitializeProcThreadAttributeList(list,attributeCount,0,ref size);
            Check.That(listInitialized&&Native.UpdateProcThreadAttribute(list,0,new IntPtr(Native.PROC_THREAD_ATTRIBUTE_HANDLE_LIST),handles,new IntPtr(inherited.Count*IntPtr.Size),IntPtr.Zero,IntPtr.Zero),"handle_list_unavailable");
            uint flags=Native.EXTENDED_STARTUPINFO_PRESENT|Native.CREATE_UNICODE_ENVIRONMENT|Native.CREATE_NO_WINDOW;
            if(isolatedMcp) {
                // Assign the Job during creation, not afterwards. Even a
                // launcher crash cannot leave an unassigned suspended child.
                job=Native.NewMcpJob();
                jobs=Marshal.AllocHGlobal(IntPtr.Size);Marshal.WriteIntPtr(jobs,job.DangerousGetHandle());
                Check.That(Native.UpdateProcThreadAttribute(list,0,new IntPtr(Native.PROC_THREAD_ATTRIBUTE_JOB_LIST),
                    jobs,new IntPtr(IntPtr.Size),IntPtr.Zero,IntPtr.Zero),"mcp_job_unavailable");
                flags|=Native.CREATE_SUSPENDED;
            }
            var startup=new Native.StartupEx();startup.startup.cb=Marshal.SizeOf(typeof(Native.StartupEx));startup.startup.flags=0x100;
            startup.startup.input=standard[0];startup.startup.output=standard[1];startup.startup.error=standard[2];startup.attributes=list;
            var command=new StringBuilder(Quote(executable));foreach(string value in args){command.Append(' ').Append(Quote(value));}Check.That(command.Length<32766,"command_too_long");
            environment=EnvironmentBlock(manager,executable,contextPath);
            // Intent must already be durably creating. Any exception after this
            // assignment is conservative unless Win32 explicitly returns FALSE.
            called=true;
            if(!Native.CreateProcessW(executable,command,IntPtr.Zero,IntPtr.Zero,true,flags,environment,cwd,ref startup,out process)) {
                returnedFailure=true;throw new Failure("child_not_created",78);
            }
            if(isolatedMcp) {
                bool member;
                Check.That(Native.IsProcessInJob(process.process,job,out member)&&member,"mcp_job_unconfirmed");
                // If the ledger cannot be confirmed, do not run any Python.
                // Closing the private Job handles cleanup without adopting a PID.
                created(Native.Identity(process.process,process.pid));
                Check.That(Native.ResumeThread(process.thread)==1,"mcp_resume_unconfirmed");
                Native.CloseHandle(process.thread);process.thread=IntPtr.Zero;
            }else {
                Native.CloseHandle(process.thread);process.thread=IntPtr.Zero;
                try { created(Native.Identity(process.process,process.pid)); }
                catch(Exception) { /* Keep waiting the real child; creating remains unknown. */ }
            }
            Check.That(Native.WaitForSingleObject(process.process,0xffffffff)==0,"child_wait_unknown");uint exit;
            Check.That(Native.GetExitCodeProcess(process.process,out exit),"child_wait_unknown");return exit;
        }
        finally {
            if(process.thread!=IntPtr.Zero)Native.CloseHandle(process.thread);
            // Only the private MCP Job can terminate on close. Other child
            // modes retain their original inherited lease and unknown policy.
            if(job!=null)job.Dispose();
            if(process.process!=IntPtr.Zero)Native.CloseHandle(process.process);
            if(environment!=IntPtr.Zero)Marshal.FreeHGlobal(environment);
            if(list!=IntPtr.Zero){if(listInitialized)Native.DeleteProcThreadAttributeList(list);Marshal.FreeHGlobal(list);}
            if(jobs!=IntPtr.Zero)Marshal.FreeHGlobal(jobs);
            if(handles!=IntPtr.Zero)Marshal.FreeHGlobal(handles);foreach(IntPtr handle in standard)if(handle!=IntPtr.Zero&&handle!=Native.Invalid)Native.CloseHandle(handle);
            GC.KeepAlive(lease);
        }
    }
    static int Product(Installation install,Gate gate,string mode,Dictionary<string,string> values) {
        SafeFileHandle lease=null;Dictionary<string,object> intent=null,child=null;bool called=false,returnedFailure=false;
        string python=Path.Combine(install.target,mode=="run"?"runtime/python/pythonw.exe":"runtime/python/python.exe");
        string script=mode=="mcp"?"app/scripts/local_mcp.py":mode=="doctor-imports"?"app/scripts/local_doctor.py":"app/scripts/local_app.py";
        var args=new List<string>{"-B",Path.Combine(install.target,script)};
        if(mode=="mcp") {args.Add("--context");args.Add(Path.Combine(install.target,"state/data/.local_app/context.json"));}
        else if(mode=="doctor-imports") {args.Add("doctor-imports");args.Add("--target");args.Add(install.target);}
        else {args.Add(mode);foreach(var pair in install.context){args.Add("--"+pair.Key);args.Add(pair.Value);}if(values.ContainsKey("open"))args.Add("--open");}
        gate.Enter();
        try {
            if(Check.Flag(Check.Get(gate.maintenance,"active")))throw new Failure("maintenance_active",75);
            install.VerifyCurrent(gate);
            lease=gate.ReadLease();lifetimeLeases.Add(lease);
            install.VerifyProduct(mode=="run"?"runtime/python/pythonw.exe":"runtime/python/python.exe");install.VerifyProduct(script);
            intent=gate.Reserve(mode);gate.Update(intent,"creating",null,false);
        }finally{gate.Leave();}
        bool recorded=false,isolatedMcp=mode=="mcp";
        try {
            uint exit=Spawn(python,args,Path.Combine(install.target,"app"),false,Path.Combine(install.target,"state/data/.local_app/context.json"),lease,
                delegate(Dictionary<string,object> identity) {child=identity;gate.Enter(IntentUpdateWaitMilliseconds);try{gate.Update(intent,"running",child,false,isolatedMcp);recorded=true;}finally{gate.Leave();}},out called,out returnedFailure,isolatedMcp);
            gate.Enter(IntentUpdateWaitMilliseconds);try {
                if(child==null)throw new Failure("child_identity_unknown",78);
                bool terminal=mode!="run"||exit==0;gate.Update(intent,terminal?"child_exited":"unknown",child,terminal);
            }finally{gate.Leave();}
            Check.That(recorded,"spawn_record_unconfirmed");return unchecked((int)exit);
        }
        catch(Exception) {
            if(!called||returnedFailure) {try{gate.Enter(IntentUpdateWaitMilliseconds);try{gate.Update(intent,"not_created",null,true);}finally{gate.Leave();}}catch(Exception){} }
            throw;
        }
    }
    static int Maintenance(Gate gate,string token) {
        SafeFileHandle exclusive=null;gate.Enter();try{exclusive=gate.Maintenance(token);}finally{gate.Leave();}
        using(exclusive) {
            Native.Write(Native.GetStdHandle(-11),Json.Bytes(M("ok",true,"state","maintenance_acquired")));
            var input=Native.GetStdHandle(-10);var bytes=new List<byte>();byte[] one=new byte[1];
            while(true) {uint count;if(!Native.ReadFile(input,one,1,out count,IntPtr.Zero)||count==0)return 0;
                if(one[0]==10)break;Check.That(bytes.Count<64,"invalid_maintenance_command");bytes.Add(one[0]);}
            string command=Encoding.ASCII.GetString(bytes.ToArray()).TrimEnd('\r');Check.That(command=="clear","invalid_maintenance_command");
            gate.Enter();try{gate.Clear(token,exclusive);}finally{gate.Leave();}
            Native.Write(Native.GetStdHandle(-11),Json.Bytes(M("ok",true,"state","maintenance_cleared")));return 0;
        }
    }
    static void VerifyManager(Pins pins,string root,Dictionary<string,object> manifest,Dictionary<string,Dictionary<string,object>> rows) {
        var expectedFiles=new HashSet<string>(StringComparer.OrdinalIgnoreCase);var expectedDirs=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(var pair in rows)if(Check.S(pair.Value,"component")=="manager") {Check.That(pair.Key.StartsWith("manager/python/",StringComparison.Ordinal)||pair.Key.StartsWith("manager/code/",StringComparison.Ordinal),"invalid_manager_component");expectedFiles.Add(pair.Key);}
        foreach(object item in Check.Array(Check.Get(manifest,"directories"))) {string path=Check.Text(item);if(path=="manager"||path.StartsWith("manager/",StringComparison.Ordinal))expectedDirs.Add(path);}
        Check.That(expectedFiles.Contains("manager/python/python.exe")&&expectedFiles.Contains("manager/code/customer_install.py")&&expectedFiles.Contains("manager/code/customer_bundle.py")&&expectedFiles.Contains("manager/code/local_doctor.py"),"manager_entry_missing");
        var observedFiles=new HashSet<string>(StringComparer.OrdinalIgnoreCase);var observedDirs=new HashSet<string>(StringComparer.OrdinalIgnoreCase);var todo=new Stack<string>();todo.Push("manager");
        while(todo.Count>0) {string name=todo.Pop();pins.Directory(Paths.Join(root,name));Check.That(observedDirs.Add(name)&&expectedDirs.Contains(name),"unlisted_manager_path");
            foreach(string path in Directory.GetFileSystemEntries(Paths.Join(root,name))) {string child=name+"/"+Path.GetFileName(path);Paths.Relative(child);FileAttributes attributes=File.GetAttributes(path);Check.That((attributes&FileAttributes.ReparsePoint)==0,"linked_manager_path");
                if((attributes&FileAttributes.Directory)!=0)todo.Push(child);else Check.That(observedFiles.Add(child)&&expectedFiles.Contains(child),"unlisted_manager_path");}
        }
        Check.That(observedFiles.SetEquals(expectedFiles)&&observedDirs.SetEquals(expectedDirs),"manager_tree_mismatch");
        foreach(string name in expectedFiles)Installation.VerifyFile(pins,root,name,rows[name]);
    }
    static int ManagerChild(Pins pins,string root,Dictionary<string,object> manifest,Dictionary<string,Dictionary<string,object>> rows,string script,List<string> args) {
        string entry="manager/code/"+script;
        Check.That(rows.ContainsKey(entry)&&Check.S(rows[entry],"component")=="manager","manager_entry_missing");
        VerifyManager(pins,root,manifest,rows);var command=new List<string>{"-B","-X","utf8",Paths.Join(root,entry)};command.AddRange(args);
        bool called,failed;uint exit=Spawn(Paths.Join(root,"manager/python/python.exe"),command,Paths.Join(root,"manager/code"),true,null,null,delegate(Dictionary<string,object> child){},out called,out failed);return unchecked((int)exit);
    }
    static int Setup(string mode,Dictionary<string,string> values) {
        string root=Paths.Absolute(Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location));
        Check.That(!Paths.Within(values["target"],root)&&!Paths.Within(root,values["target"]),"bundle_target_containment");
        using(var pins=new Pins()) {
            byte[] raw=pins.Read(Path.Combine(root,"bundle.manifest.json"),8*1024*1024,true);Check.That(Hash(raw)==values["manifest-sha256"],"manifest_mismatch");
            var manifest=Check.Map(Json.Parse(raw));var rows=new Dictionary<string,Dictionary<string,object>>(StringComparer.Ordinal);Installation.ValidateManifest(manifest,rows);
            Check.That(rows.ContainsKey("CustomerSetup.exe")&&Check.S(rows["CustomerSetup.exe"],"component")=="bootstrap","bootstrap_unlisted");Installation.VerifyFile(pins,root,"CustomerSetup.exe",rows["CustomerSetup.exe"]);
            if(mode=="doctor") {var options=new List<string>{"doctor","--target",values["target"]};if(values.ContainsKey("deep"))options.Add("--deep");return ManagerChild(pins,root,manifest,rows,"local_doctor.py",options);}
            if(TransactionMode(mode)) {
                Check.That(Check.S(manifest,"version")==Installation.TransactionVersion,"transaction_bundle_required");
                var options=new List<string>{mode,"--bundle",root,"--target",values["target"],"--manifest-sha256",values["manifest-sha256"]};
                return ManagerChild(pins,root,manifest,rows,"customer_upgrade.py",options);
            }
            var args=new List<string>{"install","--bundle",root,"--target",values["target"],"--manifest-sha256",values["manifest-sha256"]};
            foreach(string key in new[]{"port","cdp-port","browser","browser-path"})if(values.ContainsKey(key)){args.Add("--"+key);args.Add(values[key]);}
            return ManagerChild(pins,root,manifest,rows,"customer_install.py",args);
        }
    }
    static int Doctor(Installation install,Dictionary<string,string> values) {
        using(var pins=new Pins()) {
            pins.Directory(install.cache);var owner=Check.Map(Json.Parse(pins.Read(Path.Combine(install.cache,"cache-owner.json"),65536,true)));
            Check.Keys(owner,"schema","kind","install_id","target","target_identity","bundle_manifest_sha256");Check.Schema(owner,"customer_manager_cache_owner");
            Check.That(Check.S(owner,"install_id")==install.id&&Paths.Same(Check.S(owner,"target"),install.target)&&Json.Dump(Check.Get(owner,"target_identity"))==Json.Dump(install.identity)&&Check.S(owner,"bundle_manifest_sha256")==install.managerManifestSha,"manager_cache_owner_mismatch");
            byte[] raw=pins.Read(Path.Combine(install.cache,"bundle.manifest.json"),8*1024*1024,true);Check.That(Hash(raw)==install.managerManifestSha,"manager_cache_manifest_mismatch");
            var manifest=Check.Map(Json.Parse(raw));var rows=new Dictionary<string,Dictionary<string,object>>(StringComparer.Ordinal);Installation.ValidateManifest(manifest,rows);
            install.VerifyManagerVersion(manifest);
            var args=new List<string>{"doctor","--target",install.target};if(values.ContainsKey("deep"))args.Add("--deep");return ManagerChild(pins,install.cache,manifest,rows,"local_doctor.py",args);
        }
    }
}
}
