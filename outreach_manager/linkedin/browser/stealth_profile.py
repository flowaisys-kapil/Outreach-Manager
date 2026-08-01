# outreach_manager/linkedin/browser/stealth_profile.py
"""
Advanced stealth fingerprint module for Outreach Manager.

Injects 13 anti-bot-detection JS scripts into every Playwright page,
covering all major detection vectors used by LinkedIn, Cloudflare,
PerimeterX, Datadome, and similar services.

Goes beyond Outreach Manager's baseline by adding:
  - Canvas fingerprint noise (session-unique seed)
  - WebGL vendor/renderer spoofing
  - navigator.userAgentData (Client Hints) spoofing
  - screen/outerWidth/outerHeight alignment
  - media codec capability spoofing
  - Function.prototype.toString native-code defense
  - iframe contentWindow webdriver patch
  - Error.captureStackTrace automation trace removal

Usage (wire in launch.py for CDP mode)::

    from outreach_manager.linkedin.browser.stealth_profile import (
        apply_full_stealth, apply_stealth_to_new_page, get_chrome_launch_args,
    )
    apply_full_stealth(page, context)
    context.on("page", apply_stealth_to_new_page)
"""
from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Consistent Fingerprint — all scripts share these values.
# Any mismatch between navigator.* and HTTP headers is itself a signal.
# ---------------------------------------------------------------------------
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_PLATFORM = "Win32"
_VENDOR = "Google Inc."
_WEBGL_VENDOR = "Google Inc. (Intel)"
_WEBGL_RENDERER = (
    "ANGLE (Intel, Intel(R) UHD Graphics 620 "
    "Direct3D11 vs_5_0 ps_5_0, D3D11)"
)
_HW_CONCURRENCY = 8
_DEVICE_MEMORY  = 8
_SCREEN_W       = 1920
_SCREEN_H       = 1080
_AVAIL_H        = 1032   # taskbar height = 48px
_COLOR_DEPTH    = 24
_SEC_CH_UA = (
    '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="99"'
)

# Session-unique canvas noise — prevents cross-run fingerprint correlation.
# Re-randomised each daemon startup (module import time).
_CANVAS_NOISE = round(random.uniform(0.15, 0.85), 6)


# ---------------------------------------------------------------------------
# Stealth Scripts — standalone IIFEs, safe to compose and run in any order.
# ---------------------------------------------------------------------------

_S_TOSTRING = """\
(function(){
  var _o=Function.prototype.toString;
  Function.prototype.toString=function(){
    if(this.__isNativeOverride)
      return 'function '+(this.__nativeName||'')+'() { [native code] }';
    return _o.apply(this,arguments);
  };
})();"""

_S_WEBDRIVER = """\
(function(){
  try{Object.defineProperty(navigator,'webdriver',{get:()=>undefined,configurable:true});}catch(e){}
  try{delete navigator.__proto__.webdriver;}catch(e){}
})();"""

_S_CHROME = """\
(function(){
  if(!window.chrome)window.chrome={};
  var r=window.chrome;
  if(!r.runtime)r.runtime={
    connect:function(){},sendMessage:function(){},getManifest:function(){return {};},id:undefined,
    PlatformOs:{MAC:'mac',WIN:'win',ANDROID:'android',CROS:'cros',LINUX:'linux',OPENBSD:'openbsd'}
  };
  if(!r.app)r.app={
    isInstalled:false,getDetails:function(){return null;},
    getIsInstalled:function(){return false;},runningState:function(){return 'cannot_run';}
  };
  if(!r.csi)r.csi=function(){
    return {startE:Date.now(),onloadT:Date.now()+~~(Math.random()*400+200),
            pageT:~~(Math.random()*5000+1000),tran:15};
  };
  if(!r.loadTimes){
    var _t=Date.now()/1000;
    r.loadTimes=function(){return {
      requestTime:_t-Math.random()*2,startLoadTime:_t-Math.random(),
      commitLoadTime:_t-Math.random()*.5,finishDocumentLoadTime:_t,
      finishLoadTime:_t+Math.random()*.2,firstPaintTime:_t-Math.random()*.1,
      firstPaintAfterLoadTime:0,navigationType:'Other',wasFetchedViaSpdy:false,
      wasNpnNegotiated:false,npnNegotiatedProtocol:'unknown',
      wasAlternateProtocolAvailable:false,connectionInfo:'http/1.1'
    };};
  }
})();"""

_S_PLUGINS = """\
(function(){
  var mt0={type:'application/x-google-chrome-pdf',suffixes:'pdf',description:'Portable Document Format'};
  var mt1={type:'application/pdf',suffixes:'pdf',description:'Portable Document Format'};
  var pl=[
    {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format',mimeTypes:[mt0]},
    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:'',mimeTypes:[mt1]},
    {name:'Native Client',filename:'internal-nacl-plugin',description:'',mimeTypes:[]},
    {name:'Microsoft Edge PDF Plugin',filename:'edge-pdf-viewer',description:'Microsoft Edge PDF Plugin',mimeTypes:[mt1]},
    {name:'WebKit built-in PDF',filename:'webkit-pdf-viewer',description:'WebKit Built-in PDF',mimeTypes:[mt1]}
  ];
  function PA(ps){
    ps.forEach(function(p,i){this[i]=p;},this);
    this.length=ps.length;
    this.item=function(i){return ps[i];};
    this.namedItem=function(n){return ps.find(function(p){return p.name===n;})||null;};
    this.refresh=function(){};
  }
  try{Object.defineProperty(navigator,'plugins',{get:function(){return new PA(pl);},configurable:true});}catch(e){}
  try{Object.defineProperty(navigator,'mimeTypes',{get:function(){
    return {length:2,0:mt0,1:mt1,item:function(i){return [mt0,mt1][i];}};
  },configurable:true});}catch(e){}
})();"""

_S_PERMS = """\
(function(){
  var oq=navigator.permissions&&navigator.permissions.query.bind(navigator.permissions);
  if(!oq)return;
  navigator.permissions.query=function(p){
    if(p.name==='notifications')return Promise.resolve({state:Notification.permission,onchange:null});
    if(/clipboard/.test(p.name))return Promise.resolve({state:'granted',onchange:null});
    return oq(p);
  };
})();"""

_S_NAV = (
    "(function(){"
    "  var d={"
    f"    platform:'{_PLATFORM}',"
    f"    vendor:'{_VENDOR}',"
    "    languages:['en-US','en'],"
    f"    hardwareConcurrency:{_HW_CONCURRENCY},"
    f"    deviceMemory:{_DEVICE_MEMORY},"
    "    appVersion:'5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',"
    "    appName:'Netscape',product:'Gecko',productSub:'20030107',"
    "    vendorSub:'',maxTouchPoints:0,onLine:true,cookieEnabled:true"
    "  };"
    "  Object.keys(d).forEach(function(k){"
    "    try{Object.defineProperty(navigator,k,{get:function(){return d[k];},configurable:true});}catch(e){}"
    "  });"
    "})();"
)

_S_SCREEN = (
    "(function(){"
    "  var d={"
    f"    width:{_SCREEN_W},height:{_SCREEN_H},"
    f"    availWidth:{_SCREEN_W},availHeight:{_AVAIL_H},"
    f"    colorDepth:{_COLOR_DEPTH},pixelDepth:{_COLOR_DEPTH}"
    "  };"
    "  Object.keys(d).forEach(function(k){"
    "    try{Object.defineProperty(screen,k,{get:function(){return d[k];},configurable:true});}catch(e){}"
    "  });"
    "  try{Object.defineProperty(screen,'orientation',{get:function(){return {type:'landscape-primary',angle:0};},configurable:true});}catch(e){}"
    f"  try{{Object.defineProperty(window,'outerWidth',{{get:function(){{return {_SCREEN_W};}},configurable:true}});}}catch(e){{}}"
    f"  try{{Object.defineProperty(window,'outerHeight',{{get:function(){{return {_SCREEN_H};}},configurable:true}});}}catch(e){{}}"
    "})();"
)

_S_WEBGL = (
    "(function(){"
    f"  var VN='{_WEBGL_VENDOR}';"
    f"  var RN='{_WEBGL_RENDERER}';"
    "  function patch(Cls){"
    "    if(!window[Cls])return;"
    "    var o=window[Cls].prototype.getParameter;"
    "    window[Cls].prototype.getParameter=function(p){"
    "      if(p===37445)return VN;if(p===37446)return RN;"
    "      return o.call(this,p);"
    "    };"
    "  }"
    "  patch('WebGLRenderingContext');patch('WebGL2RenderingContext');"
    "})();"
)

# Canvas noise — _CANVAS_NOISE placeholder is substituted at apply time
_S_CANVAS_TMPL = """\
(function(){
  var n=_CANVAS_NOISE;
  var oTDU=HTMLCanvasElement.prototype.toDataURL;
  var oGID=CanvasRenderingContext2D.prototype.getImageData;
  HTMLCanvasElement.prototype.toDataURL=function(){
    var ctx=this.getContext('2d');
    if(ctx){
      var d=ctx.getImageData(0,0,this.width,this.height);
      for(var i=0;i<d.data.length;i+=4){
        d.data[i]=Math.min(255,d.data[i]+~~(n*2-1));
        d.data[i+1]=Math.min(255,d.data[i+1]+~~(n*2-1));
      }
      ctx.putImageData(d,0,0);
    }
    return oTDU.apply(this,arguments);
  };
  CanvasRenderingContext2D.prototype.getImageData=function(){
    var d=oGID.apply(this,arguments);
    for(var i=0;i<d.data.length;i+=4){
      d.data[i]=Math.min(255,d.data[i]+~~((n*i)%2));
      d.data[i+1]=Math.min(255,d.data[i+1]+~~((n*i)%2));
    }
    return d;
  };
})();"""

_S_CODECS = """\
(function(){
  var ov=HTMLVideoElement.prototype.canPlayType;
  HTMLVideoElement.prototype.canPlayType=function(t){
    if(/ogg|vorbis|theora|webm|vp8|vp9|mp4|h264|avc|aac/.test(t))return 'probably';
    return ov.apply(this,arguments);
  };
  var oa=HTMLAudioElement.prototype.canPlayType;
  HTMLAudioElement.prototype.canPlayType=function(t){
    if(/ogg|vorbis|mp3|mpeg|mp4|aac|wav/.test(t))return 'probably';
    return oa.apply(this,arguments);
  };
})();"""

_S_IFRAME = """\
(function(){
  try{
    Object.defineProperty(HTMLIFrameElement.prototype,'contentWindow',{
      get:function(){
        var w=this.__contentWindow||(this.contentDocument&&this.contentDocument.defaultView);
        if(w){try{Object.defineProperty(w.navigator,'webdriver',{get:function(){return undefined;},configurable:true});}catch(e){}}
        return w;
      },configurable:true
    });
  }catch(e){}
})();"""

_S_ERROR = """\
(function(){
  if(!Error.captureStackTrace)return;
  var o=Error.captureStackTrace;
  Error.captureStackTrace=function(t,c){
    o(t,c);
    if(t.stack)t.stack=t.stack.replace(/\\s+at (playwright|Protocol|CDPSession).*/gi,'');
  };
})();"""

_S_UADATA = """\
(function(){
  if(!navigator.userAgentData)return;
  try{
    var brands=[
      {brand:'Chromium',version:'125'},
      {brand:'Google Chrome',version:'125'},
      {brand:'Not-A.Brand',version:'99'}
    ];
    Object.defineProperty(navigator,'userAgentData',{get:function(){return {
      brands:brands,mobile:false,platform:'Windows',
      getHighEntropyValues:function(hints){
        var m={brands:brands,mobile:false,platform:'Windows',
          platformVersion:'10.0.0',architecture:'x86',bitness:'64',model:'',
          uaFullVersion:'125.0.6422.60',
          fullVersionList:brands.map(function(b){return {brand:b.brand,version:'125.0.6422.60'};})
        };
        return Promise.resolve(Object.fromEntries(hints.map(function(h){return [h,m[h]];})));
      },
      toJSON:function(){return {brands:brands,mobile:false,platform:'Windows'};}
    };},configurable:true});
  }catch(e){}
})();"""


# Ordered list — tostring/webdriver must come before anything that patches
# native functions; chrome before permissions.
_ALL_SCRIPTS: list[tuple[str, str]] = [
    ("tostring",  _S_TOSTRING),
    ("webdriver", _S_WEBDRIVER),
    ("chrome",    _S_CHROME),
    ("plugins",   _S_PLUGINS),
    ("perms",     _S_PERMS),
    ("navigator", _S_NAV),
    ("screen",    _S_SCREEN),
    ("webgl",     _S_WEBGL),
    ("canvas",    _S_CANVAS_TMPL),   # noise placeholder substituted at apply time
    ("codecs",    _S_CODECS),
    ("iframe",    _S_IFRAME),
    ("error",     _S_ERROR),
    ("uadata",    _S_UADATA),
]


def apply_full_stealth(page, context=None) -> None:
    """Inject all stealth scripts into a Playwright page.

    Call immediately after obtaining a page reference. For CDP-connected
    browsers, also register the context handler so every new tab gets stealth::

        context.on("page", apply_stealth_to_new_page)

    Args:
        page:    Playwright sync ``Page`` object.
        context: Playwright sync ``BrowserContext`` (optional — used to set
                 consistent HTTP headers and accept-language).
    """
    if context:
        try:
            context.set_extra_http_headers({
                "user-agent":         _UA,
                "sec-ch-ua":          _SEC_CH_UA,
                "sec-ch-ua-mobile":   "?0",
                "sec-ch-ua-platform": '"Windows"',
                "accept-language":    "en-US,en;q=0.9",
            })
        except Exception as exc:
            logger.debug("set_extra_http_headers failed: %s", exc)

    for name, script in _ALL_SCRIPTS:
        # Substitute the live canvas noise value into the canvas template
        actual_script = (
            script.replace("_CANVAS_NOISE", str(_CANVAS_NOISE))
            if name == "canvas"
            else script
        )
        try:
            page.add_init_script(script=actual_script)
            logger.debug("Stealth script applied: %s", name)
        except Exception as exc:
            logger.warning("Stealth script '%s' failed: %s", name, exc)

    logger.info(
        "Full stealth applied — %d scripts, canvas_noise=%.6f",
        len(_ALL_SCRIPTS),
        _CANVAS_NOISE,
    )


def apply_stealth_to_new_page(page) -> None:
    """Context event handler — fires stealth on every new browser tab.

    Register via::

        context.on("page", apply_stealth_to_new_page)
    """
    apply_full_stealth(page)


def get_chrome_launch_args(debug_port: int = 9222, profile_dir: str = "") -> list[str]:
    """Return the full hardened Chrome CLI arg list for stealth launching.

    These flags go beyond Outreach Manager's baseline and are drawn from
    bot-evasion research and the Chromium source.

    Args:
        debug_port:  Remote debugging port (default 9222).
        profile_dir: Absolute path for the dedicated ``--user-data-dir``.

    Returns:
        List[str] of Chrome CLI flags.
    """
    args = [
        f"--remote-debugging-port={debug_port}",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--no-first-run",
        "--no-default-browser-check",
        "--metrics-recording-only",
        "--use-mock-keychain",
        "--mute-audio",
        "--hide-scrollbars",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--password-store=basic",
        f"--window-size={_SCREEN_W},{_SCREEN_H}",
    ]
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
    return args
