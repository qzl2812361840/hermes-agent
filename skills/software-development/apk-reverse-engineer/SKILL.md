---
name: apk-reverse-engineer
description: APK decompilation — jadx + apktool, reverse DEX to Java.
version: "1.0"
author: Hermes / adapted from Operit ApkReverseEngineer (LGPLv3)
license: LGPLv3
metadata:
  hermes:
    tags: ["apk", "reverse", "decompile", "android"]
    category: software-development
---

# APK Reverse Engineer Skill

Decompile and analyze Android APK files using jadx + apktool.
For local/development use only — NOT integrated into the ganiu app.

## Tools

| Tool | Path | Function |
|---|---|---|
| jadx v1.5.3 | `/usr/local/bin/jadx` | Decompile DEX → Java source |
| apktool v2.11.0 | `/usr/local/bin/apktool.jar` | Unpack/repack APK resources |

## How to Use

### Decompile APK to Java source
```bash
jadx -d /d/tmp/apk-output /path/to/app.apk
```
Output: Java source code in `/d/tmp/apk-output/sources/`

### Decompile APK to Smali + Resources
```bash
java -jar /usr/local/bin/apktool.jar d /path/to/app.apk -o /d/tmp/apk-unpacked
```
Output: Smali code + resources in `/d/tmp/apk-unpacked/`

### Inspect AndroidManifest
```bash
java -jar /usr/local/bin/apktool.jar d -s /path/to/app.apk -o /d/tmp/manifest-only
cat /d/tmp/manifest-only/AndroidManifest.xml
```

### Quick DEX class list
```bash
jadx -d /d/tmp/apk-output --no-res /path/to/app.apk
find /d/tmp/apk-output/sources -name "*.java" | head -50
```

### Extract strings from DEX
```bash
strings /path/to/app.apk | grep -E '^\S{4,}' | sort -u > /d/tmp/apk-strings.txt
```

## Target APK
Default target: `/d/ga_niu_app/build/app/outputs/flutter-apk/嘎妞.apk`

## Common Use Cases

1. **检查 APK 功能是否进包**: `jadx -d ...` → grep 关键类名
2. **对比新旧版本差异**: 分别反编译 → diff 目录
3. **提取资源文件**: apktool d → res/ 目录
4. **审计第三方库**: jadx → 搜 com/ 包名
5. **验证代码完整性**: 反编译后 grep 核心方法签名

## Verification
- jadx: `jadx --version`
- apktool: `java -jar /usr/local/bin/apktool.jar --version`
- Java: `java -version`
