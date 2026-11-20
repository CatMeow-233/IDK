import Spec

/-!
Trusted vector generator (protected): prints `x lo hi ok r` / `x lo hi err 0`
lines computed by the Lean model for every combination of interesting int32
values (11^3 = 1331 vectors).  Run by the `lean` gate with `lean --run`.
-/

open LeanClamp

def samples : List Int :=
  [INT32_MIN, INT32_MIN + 1, -1000, -7, -1, 0, 1, 7, 1000, INT32_MAX - 1, INT32_MAX]

def main : IO Unit := do
  IO.println "# x lo hi expected"
  for x in samples do
    for lo in samples do
      for hi in samples do
        match clamp x lo hi with
        | none => IO.println s!"{x} {lo} {hi} err 0"
        | some r => IO.println s!"{x} {lo} {hi} ok {r}"
