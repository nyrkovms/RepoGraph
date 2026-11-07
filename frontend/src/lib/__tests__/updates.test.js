import { attentionReason } from "../updates";

describe("attentionReason", () => {
  it("names the worst flag first", () => {
    expect(attentionReason(["major", "yanked"])).toBe("A release was withdrawn by its publisher.");
    expect(attentionReason(["deprecated", "major", "vulnerability_added"])).toBe("A new security advisory was published.");
    expect(attentionReason(["removed", "unavailable"])).toBe("The package is no longer available at its source.");
    expect(attentionReason(["major", "removed"])).toBe("A release disappeared from the source.");
  });

  it("has a sentence for each of the six flags", () => {
    expect(attentionReason(["deprecated"])).toBe("A release is marked deprecated.");
    expect(attentionReason(["major"])).toBe("New major version: expect breaking changes.");
  });

  it("is empty without a known flag", () => {
    expect(attentionReason([])).toBe("");
  });
});
