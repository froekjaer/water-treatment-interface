(*
  PUMP_CONTROL — starter program for the waterworks OpenPLC integration.

  Implements the same control strategy as the emulator's internal Python PLC
  (see emulator/plc.py and the live ladder monitor in the HMI), in IEC 61131-3
  Structured Text. OpenPLC Editor v4 can also show/edit this as Ladder Diagram
  — the five rungs correspond 1:1 to the HMI ladder monitor:

    Rung 1  AUTO start request:   AUTO & Level<Start & NOT PumpCmd -> M_START
    Rung 2  AUTO seal-in:         AUTO & PumpCmd & NOT Level>Stop  -> M_KEEP
    Rung 3  MANUAL command:       NOT AUTO & ManualCmd             -> M_MAN
    Rung 4  Pump request:         M_START OR M_KEEP OR M_MAN       -> M_REQ
    Rung 5  Interlocks:           M_REQ & NOT MaxRun & NOT MinOff  -> PumpCmd

  I/O comes from the Modbus field device (emulator/modbus_bridge.py).
  Map the variables below to the bridge's registers in the OpenPLC Editor's
  Modbus client configuration:

    TankLevelPct  <- input register IR0   (tank level, % x 10)
    PressureBar   <- input register IR2   (network pressure, bar x 100)
    PumpFlowM3h   <- input register IR4   (pump flow, m3/h x 100)
    TurbidityNtu  <- input register IR5   (treated turbidity, NTU x 1000)
    PumpRunning   <- discrete input D0    (pump physical feedback)
    PumpCmd       -> coil C0              (pump command, written by the PLC)
*)

PROGRAM PUMP_CONTROL

VAR
    (* ── I/O mapped to the Modbus field device ── *)
    TankLevelPct  AT %IW0 : INT;     (* tank level [% x10] *)
    PressureBar   AT %IW1 : INT;     (* network pressure [bar x100] *)
    PumpFlowM3h   AT %IW2 : INT;     (* pump flow [m3/h x100] *)
    TurbidityNtu  AT %IW3 : INT;     (* treated turbidity [NTU x1000] *)
    PumpRunning   AT %IX64.0 : BOOL; (* pump running feedback *)
    PumpCmd       AT %QX64.0 : BOOL; (* pump command output *)

    (* ── Setpoints (defaults match the emulator's internal PLC) ── *)
    StartPct      : INT := 350;      (* pump start level [% x10] = 35.0% *)
    StopPct       : INT := 850;      (* pump stop level  [% x10] = 85.0% *)
    MaxRuntimeMin : INT := 240;      (* max continuous runtime [min] *)
    MinOffMin     : INT := 10;       (* short-cycle protection [min] *)
    LowPressBar   : INT := 250;      (* low pressure alarm [bar x100] *)
    HighTurbNtu   : INT := 500;      (* high turbidity alarm [NTU x1000] *)

    (* ── Mode ── *)
    AutoMode      : BOOL := TRUE;
    ManualCmd     : BOOL := FALSE;

    (* ── Internal relays (the M-bits from the ladder monitor) ── *)
    M_START       : BOOL;
    M_KEEP        : BOOL;
    M_MAN         : BOOL;
    M_REQ         : BOOL;

    (* ── Interlock timers ── *)
    TON_Run       : TON;             (* measures continuous runtime *)
    TON_Off       : TON;             (* measures off-time *)
    MaxRunTrip    : BOOL;
    MinOffTrip    : BOOL;

    (* ── Alarms ── *)
    ALM_LowPressure   : BOOL;
    ALM_HighTurbidity : BOOL;
    ALM_PumpFault     : BOOL;
END_VAR

(* ── Rung 1: AUTO start request ───────────────────────────────────────── *)
M_START := AutoMode AND NOT PumpCmd AND (TankLevelPct < StartPct);

(* ── Rung 2: AUTO seal-in (keep running until stop level) ─────────────── *)
M_KEEP := AutoMode AND PumpCmd AND (TankLevelPct <= StopPct);

(* ── Rung 3: MANUAL operator command ──────────────────────────────────── *)
M_MAN := NOT AutoMode AND ManualCmd;

(* ── Rung 4: pump request = start OR keep OR manual ───────────────────── *)
M_REQ := M_START OR M_KEEP OR M_MAN;

(* ── Interlock timers ─────────────────────────────────────────────────── *)
TON_Run(IN := PumpCmd, PT := T#1m * MaxRuntimeMin);
TON_Off(IN := NOT PumpCmd, PT := T#1m * MinOffMin);
MaxRunTrip := M_REQ AND TON_Run.Q;     (* continuous runtime exceeded *)
MinOffTrip := M_REQ AND NOT PumpCmd AND NOT TON_Off.Q;  (* too short off-time *)

(* ── Rung 5: interlocks always win ────────────────────────────────────── *)
PumpCmd := M_REQ AND NOT MaxRunTrip AND NOT MinOffTrip;

(* ── Alarms (simple direct evaluation; add delays as needed) ──────────── *)
ALM_LowPressure   := PressureBar < LowPressBar;
ALM_HighTurbidity := TurbidityNtu > HighTurbNtu;
ALM_PumpFault     := PumpCmd AND PumpRunning AND (PumpFlowM3h < 50);

END_PROGRAM


CONFIGURATION Config0
    RESOURCE Res0 ON PLC
        TASK MainTask(INTERVAL := T#100ms, PRIORITY := 0);
        PROGRAM Main WITH MainTask : PUMP_CONTROL;
    END_RESOURCE
END_CONFIGURATION
