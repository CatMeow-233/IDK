/-!
# Trusted Lean model of `clamp_int` (protected: part of the approved spec)

`clamp x lo hi` is the mathematical meaning of `clamp_int(x, lo, hi, &out)` on
unbounded integers: `some r` means the C function returns `CLAMP_OK` and
writes `r` to `*out`; `none` means it returns `CLAMP_ERR_RANGE` and leaves
`*out` untouched.  (`out == NULL` is a C-level concern checked by CBMC.)

The theorem *statements* below are what the editable proof file
`lean/Proofs.lean` must prove.  They are statements about this MODEL: the
model is linked to the C code only by the differential test vectors printed
by `lean/Vectors.lean`.
-/
namespace LeanClamp

def clamp (x lo hi : Int) : Option Int :=
  if hi < lo then none else some (max lo (min x hi))

def INT32_MIN : Int := -2147483648
def INT32_MAX : Int := 2147483647

/-- POST1: a successful result lies in `[lo, hi]`. -/
def result_in_bounds : Prop :=
  ∀ x lo hi r : Int, clamp x lo hi = some r → lo ≤ r ∧ r ≤ hi

/-- POST3: the error result occurs exactly for an empty interval. -/
def error_iff_empty_range : Prop :=
  ∀ x lo hi : Int, clamp x lo hi = none ↔ hi < lo

/-- POST2: values already inside the interval are returned unchanged. -/
def identity_in_range : Prop :=
  ∀ x lo hi : Int, lo ≤ x → x ≤ hi → clamp x lo hi = some x

/-- Clamping twice is the same as clamping once. -/
def idempotent : Prop :=
  ∀ x lo hi r : Int, clamp x lo hi = some r → clamp r lo hi = some r

/-- Clamping is monotone in `x`. -/
def monotone : Prop :=
  ∀ x y lo hi rx ry : Int, x ≤ y → clamp x lo hi = some rx → clamp y lo hi = some ry → rx ≤ ry

/-- With 32-bit bounds the result is a 32-bit value (no overflow is possible). -/
def result_fits_int32 : Prop :=
  ∀ x lo hi r : Int, INT32_MIN ≤ lo → hi ≤ INT32_MAX → clamp x lo hi = some r →
    INT32_MIN ≤ r ∧ r ≤ INT32_MAX

end LeanClamp
