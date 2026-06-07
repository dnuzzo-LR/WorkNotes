Here's how to get LabChirp running under Bottles on Fedora 44:


### Step 1 — Create a Bottle

1. Launch Bottles: `flatpak run com.usebottles.bottles`
2. Click **"Create a Bottle"**
3.  Use Advanced Tab to create a 32bit Bottle
4. Name it something like `LabChirp`
5. Choose the **"Application"** environment type
6. Click **Create** and wait for it to finish downloading Wine components

---

### Step 2 — Install .NET 2.0 (required by LabChirp)

Bottles lets you install critical dependencies like .NET with a single click.

Inside your bottle:
1. Click **"Dependencies"**
2. Search for `dotnet20` and install it (you may also want `dotnet40` as a fallback)

---

### Step 3 — Run LabChirp

Click "Run Executable" and browse to your `LabChirp.exe` file. Since LabChirp is a standalone exe (no installer needed), it should launch directly.

If you want a persistent shortcut:
1. After running it once, go to **"Programs"** in the bottle
2. It should appear there — you can add it as a shortcut to your desktop or app menu

---

### Tip: If it still has trouble with .NET

Inside the bottle, try going to **Dependencies → dotnet462** — newer .NET versions can sometimes satisfy older .NET 2.0 apps that don't work with the exact version.
















