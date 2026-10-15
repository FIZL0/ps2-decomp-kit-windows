//! VU0's model, and above all where it differs from the FPU's — because a port that uses one where the
//! original used the other is wrong by a few ulps per operation, which is the hardest class of bug to
//! find later.

use ps2_float::vu::{self, Dest, Field, Lane, Vf, lanes, v4};
use ps2_float::{F, f, fpu};

#[test]
fn the_vu_adder_is_full_width_where_the_fpus_is_not() {
    // The FPU's alignment throws the addend away; VU0 keeps it and then chops the sum.
    let tiny = f32::from_bits(0x3340_0000); // 1.5·2^-25, 25 exponents below 1.0
    assert_eq!(fpu::sub(1.0, tiny), 1.0, "the FPU cannot see it");
    assert_eq!(vu::sub(1.0, tiny), 0.99999994, "VU0 can");
    assert_eq!(vu::add(1.0, tiny), 1.0, "but still chops the sum toward zero");

    // Far apart enough that the exact sum does not fit in an f64 at all, which is why the model uses a
    // TwoSum: the direction of the truncation still has to be right.
    let far = f32::from_bits(0x0080_0000); // 2^-126
    assert_eq!(vu::add(f32::MAX, far), f32::MAX);
    assert_eq!(vu::sub(1.0, far), 0.99999994, "a tiny negative addend still moves 1.0 down one ulp");
    assert_eq!(vu::sub(f32::MAX, far), f32::from_bits(0x7f7f_fffe));
}

#[test]
fn vu_arithmetic_chops_and_clamps() {
    // 0.1·0.1 is exactly 0.01f32 on a VU, one ulp below the correctly-rounded product
    assert_eq!(vu::mul(0.1, 0.1), 0.01f32);
    assert_ne!(vu::mul(0.1, 0.1), 0.1f32 * 0.1f32, "the host rounds to nearest");
    assert_eq!(vu::mul(f32::MAX, 2.0), vu::MAX, "clamp instead of an infinity");
    assert_eq!(vu::mul(-f32::MAX, 2.0), -vu::MAX);
    assert_eq!(vu::div(1.0, 0.0), vu::MAX);
    assert_eq!(vu::div(-1.0, 0.0), -vu::MAX);
    assert_eq!(vu::div(1.0, 3.0), 0.3333333, "Q is chopped too, unlike the FPU's div.s");
    assert_eq!(fpu::div(1.0, 3.0), 0.33333334, "— which is the FPU's answer, one ulp up");
    assert_eq!(vu::sqrt(-4.0), 2.0, "vsqrt takes |x|");
    assert_eq!(vu::rsqrt(1.0, 4.0), 0.5);
    assert_eq!(vu::operand(0x7f80_0000), vu::MAX, "an Inf pattern is just a big number");
    assert_eq!(vu::operand(0x0040_0000), 0.0, "denormals are zero here too");
    assert_eq!(vu::madd(1.0, 1.0 / 3.0, 3.0), vu::add(1.0, vu::mul(1.0 / 3.0, 3.0)), "vmadd is not fused");
}

#[test]
fn field_masks_leave_the_other_lanes_alone() {
    let old = Vf::new(1.0, 2.0, 3.0, 4.0);
    let new = Vf::splat(9.0);
    assert_eq!(old.merge(Dest::XYZ, new).0, [9.0, 9.0, 9.0, 4.0]);
    assert_eq!(old.merge(Dest::W, new).0, [1.0, 2.0, 3.0, 9.0]);
    assert_eq!(old.merge(Dest::XZ, new).0, [9.0, 2.0, 9.0, 4.0]);
    assert_eq!(old.merge(Dest::XYZW, new).0, [9.0; 4]);
    assert_eq!(old.merge(Dest(0), new).0, old.0);
    assert_eq!(Dest::XYZ.name(), "xyz");
    assert_eq!(Dest::W.name(), "w");
    assert_eq!(Dest::XZ.name(), "xz");
    assert!(Dest::YZW.has(3) && !Dest::YZW.has(0));
    // a broadcast operand (vmulx, vaddw, …) is one lane in all four
    assert_eq!(old.bc(Field::Y).0, [2.0; 4]);
    assert_eq!(old.lane(Field::W), 4.0);
}

#[test]
fn the_cross_product_idiom_matches_its_two_instructions() {
    let (a, b) = ([1.0, 2.0, 3.0, 0.0], [-4.0, 5.0, -6.0, 0.0]);
    assert_eq!(vu::cross(a, b), [-27.0, -6.0, 13.0, 0.0]);
    // opmula/opmsub share one lane pattern; the cross product comes from the swapped operands
    let acc = Vf::opmula(Vf(a), Vf(b));
    assert_eq!(acc.0, [vu::mul(2.0, -6.0), vu::mul(3.0, -4.0), vu::mul(1.0, 5.0), 0.0]);
    assert_eq!(Vf::opmsub(acc, Vf(b), Vf(a)).0[..3], vu::cross(a, b)[..3]);
}

#[test]
fn fixed_point_conversions_truncate_and_saturate() {
    let v = Vf::new(1.5, -1.5, 0.25, -0.25);
    assert_eq!(v.ftoi(0), [1, -1, 0, 0], "vftoi0 truncates toward zero");
    assert_eq!(v.ftoi(4), [24, -24, 4, -4], "vftoi4 is 12.4 fixed point");
    assert_eq!(v.ftoi(12), [6144, -6144, 1024, -1024]);
    assert_eq!(Vf::splat(1.0e30).ftoi(12), [i32::MAX; 4], "saturates, never wraps");
    assert_eq!(Vf::splat(-1.0e30).ftoi(12), [i32::MIN; 4]);
    assert_eq!(Vf::itof([6144, -1024, 1, 0], 12).0, [1.5, -0.25, 0.00024414062, 0.0]);
    assert_eq!(Vf::itof(v.ftoi(12), 12).0, v.0, "round trip at 12 fractional bits");
}

#[test]
fn the_lane_wrapper_reads_like_a_vu_listing() {
    let (a, b) = (Lane(1.0), Lane(1.0 / 3.0));
    assert_eq!((a + b * 3.0).0, vu::add(1.0, vu::mul(1.0 / 3.0, 3.0)));
    assert_eq!(a.madd(b, Lane(3.0)).0, vu::madd(1.0, 1.0 / 3.0, 3.0));
    assert_eq!(v4(lanes([1.0, 2.0, 3.0, 4.0])), [1.0, 2.0, 3.0, 4.0]);
    // dot3 accumulates x, then y, then z, in that order
    assert_eq!(vu::dot3([1.0, 1.0, 1.0, 0.0], [1.0 / 3.0; 4]), 1.0);
    assert_eq!(vu::dot3([1.0, 2.0, 3.0, 9.0], [4.0, 5.0, 6.0, 9.0]), 32.0, "w is not in it");
    // sqrt, reciprocal and multiply each chop, so a 3-4-5 vector normalises to just *under* 0.6/0.8 —
    // this is the drift a port inherits whether it wants to or not, and it has to inherit it exactly
    assert_eq!(vu::normalize([3.0, 4.0, 0.0, 7.0]), [0.59999996, 0.79999995, 0.0, 0.0]);
    // the whole point of having two models: the same expression, two answers
    let tiny = -f32::from_bits(0x3340_0000); // -1.5·2^-25
    assert_eq!((Lane(1.0) + Lane(tiny)).0, 0.99999994);
    assert_eq!((f(1.0) + F(tiny)).0, 1.0, "the same expression, two answers, one unit apart");
}

#[test]
fn the_transform_idiom_keeps_the_accumulate_order() {
    let m = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [5.0, 6.0, 7.0, 1.0]];
    assert_eq!(vu::transform(&m, [1.0, 2.0, 3.0, 1.0]), [6.0, 8.0, 10.0, 1.0]);
    // a third of each row, accumulated in the instruction order, is not the same as summing it any
    // other way — which is exactly why the idiom is spelled out rather than written with an iterator
    let m3 = [[1.0 / 3.0; 4]; 4];
    assert_eq!(vu::transform(&m3, [1.0; 4])[0], vu::madd(vu::madd(vu::madd(
        vu::mul(1.0 / 3.0, 1.0), 1.0 / 3.0, 1.0), 1.0 / 3.0, 1.0), 1.0 / 3.0, 1.0));
    assert_eq!(vu::lerp([0.0; 4], [1.0; 4], 0.25), [0.75; 4]);
}
