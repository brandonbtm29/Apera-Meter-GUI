import pandas as pd
import argparse
import os


def clean_data(input_csv, output_csv):
    try:
        df = pd.read_csv(input_csv, on_bad_lines="skip")
    except Exception as e:
        print(f"Error reading {input_csv}: {e}")
        return

    if "Cond (mS/cm)" not in df.columns:
        print("Error: 'Cond (mS/cm)' column not found.")
        return

    # Convert to numeric, coerce errors
    df["Cond (mS/cm)"] = pd.to_numeric(df["Cond (mS/cm)"], errors="coerce")

    # We will iterate and build a fixed column
    fixed_cond = df["Cond (mS/cm)"].copy()

    # Track the current multiplier state
    multiplier = 1.0

    # Look for sudden 100x to 10000x drops or spikes between consecutive valid readings

    last_valid = None

    for i in range(len(fixed_cond)):
        val = fixed_cond.iloc[i]
        if pd.isna(val) or val == 0:
            continue

        # Apply the current active multiplier
        val_adj = val * multiplier

        if last_valid is not None:
            ratio = val_adj / last_valid

            # If it suddenly dropped by roughly 1000x (e.g. ratio < 0.05)
            if ratio < 0.05:
                print(f"Detected 1000x drop at row {i}. Adjusting multiplier up by 1000x.")
                multiplier *= 1000.0
                val_adj = val * multiplier

            # If it suddenly spiked by roughly 1000x (e.g. ratio > 20.0)
            elif ratio > 20.0:
                print(f"Detected 1000x spike at row {i}. Adjusting multiplier down by 1000x.")
                multiplier /= 1000.0
                val_adj = val * multiplier

        fixed_cond.iloc[i] = val_adj
        last_valid = val_adj

    df["Cond (mS/cm) fixed"] = fixed_cond
    df.to_csv(output_csv, index=False)
    print(f"Successfully wrote cleaned data to {output_csv}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix 1000x unit gaps in conductivity logs.")
    parser.add_argument("input_csv", help="Path to the input CSV file")
    parser.add_argument("output_csv", nargs="?", help="Path to the output CSV file (optional)")
    args = parser.parse_args()

    out_file = args.output_csv
    if not out_file:
        base, ext = os.path.splitext(args.input_csv)
        out_file = f"{base}_cleaned{ext}"

    clean_data(args.input_csv, out_file)
